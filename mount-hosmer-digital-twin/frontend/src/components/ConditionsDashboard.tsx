"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  type ForecastPayload,
  type SnowPayload,
  type WeatherPayload,
  fetchJson,
} from "@/lib/api";

type LoadState = {
  weather?: WeatherPayload;
  snow?: SnowPayload;
  forecast?: ForecastPayload;
  loading: boolean;
  error?: string;
};

function formatDate(value?: string | null) {
  if (!value) {
    return "Unavailable";
  }
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatNumber(value: number | null | undefined, digits = 1) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "n/a";
}

function chartDate(value: string) {
  return value.slice(0, 10);
}

function compactRegionName(value?: string | null) {
  if (!value) {
    return "Unavailable";
  }
  if (value.length < 120) {
    return value;
  }
  return `${value.slice(0, 120)}...`;
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
      <div className="text-xs uppercase text-[var(--muted)]">{label}</div>
      <div className="mt-2 text-xl font-semibold">{value}</div>
    </div>
  );
}

function ChartPanel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
      <h3 className="text-lg font-semibold">{title}</h3>
      <div className="mt-3 h-[260px]">{children}</div>
    </div>
  );
}

export function ConditionsDashboard() {
  const [state, setState] = useState<LoadState>({ loading: true });
  const [weatherStation, setWeatherStation] = useState<string>("");
  const [snowStation, setSnowStation] = useState<string>("2C09Q");
  const [selectedEvent, setSelectedEvent] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [weather, snow, forecast] = await Promise.all([
          fetchJson<WeatherPayload>("/api/weather"),
          fetchJson<SnowPayload>("/api/snow"),
          fetchJson<ForecastPayload>("/api/avalanche-forecast"),
        ]);
        if (cancelled) {
          return;
        }
        setWeatherStation(weather.default_station_key ?? weather.stations[0]?.station_key ?? "");
        setSnowStation(snow.stations[0]?.station_id ?? "2C09Q");
        setSelectedEvent(Object.keys(weather.event_windows).sort().at(-1) ?? "");
        setState({ weather, snow, forecast, loading: false });
      } catch (err) {
        if (!cancelled) {
          setState({ loading: false, error: err instanceof Error ? err.message : "Failed to load dynamic conditions" });
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const eventDate = selectedEvent ? state.weather?.event_windows[selectedEvent]?.event_time_utc.slice(0, 10) : undefined;

  const weatherDaily = useMemo(() => {
    return (state.weather?.daily_series ?? [])
      .filter((record) => record.station_key === weatherStation)
      .map((record) => ({
        ...record,
        date: chartDate(record.timestamp_utc),
        precipitation_total_mm: record.precipitation_mm ?? 0,
        snowfall_total_cm: record.snowfall_cm ?? 0,
      }));
  }, [state.weather, weatherStation]);

  const weatherHourly = useMemo(() => {
    return (state.weather?.hourly_recent_series ?? [])
      .filter((record) => record.station_key === weatherStation)
      .map((record) => ({ ...record, date: record.timestamp_utc.slice(5, 16).replace("T", " ") }));
  }, [state.weather, weatherStation]);

  const snowSeries = useMemo(() => {
    return (state.snow?.series ?? [])
      .filter((record) => record.station_id === snowStation)
      .map((record) => ({
        ...record,
        date: chartDate(record.timestamp_utc),
        temperature_c: record.air_temperature_c ?? record.temperature_max_c ?? record.temperature_min_c ?? null,
      }));
  }, [state.snow, snowStation]);

  const currentWeatherStation = state.weather?.stations.find((station) => station.station_key === weatherStation);
  const currentSnowStation = state.snow?.stations.find((station) => station.station_id === snowStation);
  const warnings = [...(state.weather?.warnings ?? []), ...(state.snow?.warnings ?? []), ...(state.forecast?.warnings ?? [])];

  if (state.loading) {
    return <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-6 text-[var(--muted)]">Loading weather, snowpack, and forecast data...</section>;
  }

  if (state.error) {
    return <section className="rounded-lg border border-[#6f3b34] bg-[#271a18] p-6 text-[#ffd8d1]">{state.error}</section>;
  }

  return (
    <section className="flex flex-col gap-5">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h2 className="text-2xl font-semibold">Weather, Snowpack, And Forecast</h2>
          <p className="mt-1 text-sm text-[var(--muted)]">
            Latest weather {formatDate(state.weather?.latest_weather_date)}. Forecast valid until {formatDate(state.forecast?.valid_until_utc)}.
          </p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row">
          <select
            value={weatherStation}
            onChange={(event) => setWeatherStation(event.target.value)}
            className="rounded-md border border-[var(--border)] bg-[var(--panel)] px-3 py-2 text-sm text-white"
            aria-label="Select weather station"
          >
            {(state.weather?.stations ?? []).map((station) => (
              <option key={station.station_key} value={station.station_key}>
                {station.station_name} ({formatNumber(station.distance_to_aoi_km, 1)} km)
              </option>
            ))}
          </select>
          <select
            value={snowStation}
            onChange={(event) => setSnowStation(event.target.value)}
            className="rounded-md border border-[var(--border)] bg-[var(--panel)] px-3 py-2 text-sm text-white"
            aria-label="Select snow station"
          >
            {(state.snow?.stations ?? []).map((station) => (
              <option key={station.station_id} value={station.station_id}>
                {station.station_name}
              </option>
            ))}
          </select>
          <select
            value={selectedEvent}
            onChange={(event) => setSelectedEvent(event.target.value)}
            className="rounded-md border border-[var(--border)] bg-[var(--panel)] px-3 py-2 text-sm text-white"
            aria-label="Select event marker"
          >
            {Object.keys(state.weather?.event_windows ?? {}).sort().map((eventId) => (
              <option key={eventId} value={eventId}>
                {eventId}
              </option>
            ))}
          </select>
        </div>
      </div>

      <section className="grid gap-4 md:grid-cols-4">
        <Metric label="Weather rows" value={state.weather?.record_count ?? 0} />
        <Metric label="Weather stations" value={state.weather?.station_count ?? 0} />
        <Metric label="Snow rows" value={state.snow?.record_count ?? 0} />
        <Metric label="Forecast status" value={state.forecast?.highest_danger?.display ?? state.forecast?.freshness.status ?? "Unknown"} />
      </section>

      <section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_390px]">
        <div className="grid gap-5">
          <section className="grid gap-5 lg:grid-cols-2">
            <ChartPanel title={`Temperature - ${currentWeatherStation?.station_name ?? "Weather station"}`}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={weatherDaily} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke="#334044" strokeDasharray="3 3" />
                  <XAxis dataKey="date" stroke="#aab7af" minTickGap={28} />
                  <YAxis stroke="#aab7af" unit=" C" width={48} />
                  <Tooltip contentStyle={{ background: "#171d1e", border: "1px solid #334044" }} />
                  <Legend />
                  {eventDate ? <ReferenceLine x={eventDate} stroke="#d6a75b" strokeDasharray="4 4" label="event" /> : null}
                  <Line type="monotone" dataKey="temperature_min_c" name="Min C" stroke="#75bfff" dot={false} connectNulls isAnimationActive={false} />
                  <Line type="monotone" dataKey="air_temperature_c" name="Mean C" stroke="#8fbc8f" dot={false} connectNulls isAnimationActive={false} />
                  <Line type="monotone" dataKey="temperature_max_c" name="Max C" stroke="#e16d5a" dot={false} connectNulls isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </ChartPanel>

            <ChartPanel title="Precipitation And Snowfall">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={weatherDaily} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke="#334044" strokeDasharray="3 3" />
                  <XAxis dataKey="date" stroke="#aab7af" minTickGap={28} />
                  <YAxis yAxisId="left" stroke="#aab7af" width={48} />
                  <YAxis yAxisId="right" orientation="right" stroke="#aab7af" width={48} />
                  <Tooltip contentStyle={{ background: "#171d1e", border: "1px solid #334044" }} />
                  <Legend />
                  {eventDate ? <ReferenceLine x={eventDate} stroke="#d6a75b" strokeDasharray="4 4" /> : null}
                  <Bar yAxisId="left" dataKey="precipitation_total_mm" name="Precip mm" fill="#5cc8ff" isAnimationActive={false} />
                  <Line yAxisId="right" type="monotone" dataKey="snowfall_total_cm" name="Snow cm" stroke="#e9f4ff" dot={false} connectNulls isAnimationActive={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </ChartPanel>

            <ChartPanel title="Wind Speed And Direction">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={weatherHourly} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke="#334044" strokeDasharray="3 3" />
                  <XAxis dataKey="date" stroke="#aab7af" minTickGap={34} />
                  <YAxis yAxisId="left" stroke="#aab7af" width={48} />
                  <YAxis yAxisId="right" orientation="right" stroke="#aab7af" width={48} />
                  <Tooltip contentStyle={{ background: "#171d1e", border: "1px solid #334044" }} />
                  <Legend />
                  <Line yAxisId="left" type="monotone" dataKey="wind_speed_kmh" name="Speed km/h" stroke="#d6a75b" dot={false} connectNulls isAnimationActive={false} />
                  <Line yAxisId="right" type="monotone" dataKey="wind_direction_degrees" name="Direction deg" stroke="#b78cff" dot={false} connectNulls isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </ChartPanel>

            <ChartPanel title={`Snowpack - ${currentSnowStation?.station_name ?? "Snow station"}`}>
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={snowSeries} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke="#334044" strokeDasharray="3 3" />
                  <XAxis dataKey="date" stroke="#aab7af" minTickGap={28} />
                  <YAxis yAxisId="left" stroke="#aab7af" width={48} />
                  <YAxis yAxisId="right" orientation="right" stroke="#aab7af" width={48} />
                  <Tooltip contentStyle={{ background: "#171d1e", border: "1px solid #334044" }} />
                  <Legend />
                  {eventDate ? <ReferenceLine x={eventDate} stroke="#d6a75b" strokeDasharray="4 4" /> : null}
                  <Area yAxisId="left" type="monotone" dataKey="snow_depth_cm" name="Snow depth cm" stroke="#e9f4ff" fill="#e9f4ff" fillOpacity={0.18} connectNulls isAnimationActive={false} />
                  <Line yAxisId="right" type="monotone" dataKey="swe_mm" name="SWE mm" stroke="#75bfff" dot={false} connectNulls isAnimationActive={false} />
                  <Line yAxisId="right" type="monotone" dataKey="temperature_c" name="Air temp C" stroke="#e16d5a" dot={false} connectNulls isAnimationActive={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </ChartPanel>
          </section>

          <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
            <h3 className="text-lg font-semibold">Station Comparison</h3>
            <div className="mt-3 overflow-x-auto">
              <table className="w-full min-w-[760px] border-collapse text-sm">
                <thead className="text-left text-xs uppercase text-[var(--muted)]">
                  <tr>
                    <th className="border-b border-[var(--border)] py-2 pr-3">Station</th>
                    <th className="border-b border-[var(--border)] py-2 pr-3">Type</th>
                    <th className="border-b border-[var(--border)] py-2 pr-3">Distance</th>
                    <th className="border-b border-[var(--border)] py-2 pr-3">Records</th>
                    <th className="border-b border-[var(--border)] py-2 pr-3">Latest</th>
                    <th className="border-b border-[var(--border)] py-2">Variables</th>
                  </tr>
                </thead>
                <tbody>
                  {(state.weather?.stations.slice(0, 8) ?? []).map((station) => (
                    <tr key={station.station_key}>
                      <td className="border-b border-[var(--border)] py-2 pr-3">{station.station_name}</td>
                      <td className="border-b border-[var(--border)] py-2 pr-3">ECCC</td>
                      <td className="border-b border-[var(--border)] py-2 pr-3 font-mono">{formatNumber(station.distance_to_aoi_km, 1)} km</td>
                      <td className="border-b border-[var(--border)] py-2 pr-3 font-mono">{station.daily_records + station.hourly_records}</td>
                      <td className="border-b border-[var(--border)] py-2 pr-3">{formatDate(station.latest_timestamp_utc)}</td>
                      <td className="border-b border-[var(--border)] py-2 text-xs text-[var(--muted)]">{station.variables_available.slice(0, 5).join(", ")}</td>
                    </tr>
                  ))}
                  {(state.snow?.stations ?? []).map((station) => (
                    <tr key={station.station_id}>
                      <td className="border-b border-[var(--border)] py-2 pr-3">{station.station_name}</td>
                      <td className="border-b border-[var(--border)] py-2 pr-3">BC Snow</td>
                      <td className="border-b border-[var(--border)] py-2 pr-3 font-mono">n/a</td>
                      <td className="border-b border-[var(--border)] py-2 pr-3 font-mono">{station.record_count}</td>
                      <td className="border-b border-[var(--border)] py-2 pr-3">{formatDate(station.date_range_utc.end)}</td>
                      <td className="border-b border-[var(--border)] py-2 text-xs text-[var(--muted)]">{station.variables_available.join(", ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>

        <aside className="flex flex-col gap-4">
          <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
            <h3 className="text-lg font-semibold">Avalanche Canada Forecast</h3>
            <div className="mt-3 rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3">
              <div className="text-xs uppercase text-[var(--muted)]">Region</div>
              <div className="mt-1 text-sm">{compactRegionName(state.forecast?.applicable_region)}</div>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-3 text-sm">
              <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3">
                <div className="text-xs uppercase text-[var(--muted)]">Issued</div>
                <div className="mt-1">{formatDate(state.forecast?.publication_time_utc)}</div>
              </div>
              <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3">
                <div className="text-xs uppercase text-[var(--muted)]">Valid Until</div>
                <div className="mt-1">{formatDate(state.forecast?.valid_until_utc)}</div>
              </div>
            </div>
            <div className="mt-3 rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3">
              <div className="text-xs uppercase text-[var(--muted)]">Highest danger</div>
              <div className="mt-1 text-lg font-semibold">{state.forecast?.highest_danger?.display ?? "Unavailable"}</div>
              {state.forecast?.confidence?.display ? <div className="mt-1 text-sm text-[var(--muted)]">Confidence: {state.forecast.confidence.display}</div> : null}
            </div>
            {state.forecast?.highlights ? <p className="mt-3 text-sm leading-relaxed text-[var(--muted)]">{state.forecast.highlights}</p> : null}
            <p className="mt-3 text-xs leading-relaxed text-[var(--muted)]">{state.forecast?.disclaimer}</p>
          </section>

          <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
            <h3 className="text-lg font-semibold">Danger Ratings</h3>
            <div className="mt-3 space-y-3">
              {(state.forecast?.danger_ratings ?? []).map((rating) => (
                <div key={`${rating.date}-${rating.date_display}`} className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3 text-sm">
                  <div className="font-medium">{rating.date_display ?? formatDate(rating.date)}</div>
                  <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                    <div>
                      <div className="text-[var(--muted)]">Alpine</div>
                      <div>{rating.ratings.alpine?.display ?? "n/a"}</div>
                    </div>
                    <div>
                      <div className="text-[var(--muted)]">Treeline</div>
                      <div>{rating.ratings.treeline?.display ?? "n/a"}</div>
                    </div>
                    <div>
                      <div className="text-[var(--muted)]">Below</div>
                      <div>{rating.ratings.below_treeline?.display ?? "n/a"}</div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
            <h3 className="text-lg font-semibold">Avalanche Problems</h3>
            {(state.forecast?.avalanche_problems.length ?? 0) > 0 ? (
              <div className="mt-3 space-y-3">
                {state.forecast?.avalanche_problems.map((problem, index) => (
                  <div key={index} className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3 text-sm">
                    <div className="font-medium">{String(problem.type ?? "Problem")}</div>
                    <p className="mt-1 text-[var(--muted)]">{String(problem.description ?? "")}</p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-2 text-sm text-[var(--muted)]">No avalanche problems are listed in the current product.</p>
            )}
          </section>

          {warnings.length ? (
            <section className="rounded-lg border border-[#6f3b34] bg-[#271a18] p-4">
              <h3 className="text-lg font-semibold text-[#ffd8d1]">Data Coverage Warnings</h3>
              <div className="mt-3 space-y-2 text-sm leading-relaxed text-[#ffd8d1]">
                {warnings.map((warning) => (
                  <p key={warning}>{warning}</p>
                ))}
              </div>
            </section>
          ) : null}
        </aside>
      </section>
    </section>
  );
}
