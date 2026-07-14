"use client";

import { useEffect, useRef, useState } from "react";
import type { AoiPayload } from "@/lib/api";

type Props = {
  aoi?: AoiPayload;
};

function bboxFromAoi(aoi: AoiPayload | undefined): [number, number, number, number] | undefined {
  const bbox = aoi?.grid?.aoi_bbox_wgs84;
  if (bbox && bbox.length === 4) {
    return [bbox[0], bbox[1], bbox[2], bbox[3]];
  }
  return undefined;
}

export function AoiMap({ aoi }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<unknown>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function setup() {
      if (!ref.current || !aoi || mapRef.current) {
        return;
      }
      try {
        const maplibre = await import("maplibre-gl");
        if (cancelled || !ref.current) {
          return;
        }
        const bbox = bboxFromAoi(aoi);
        const center: [number, number] = bbox
          ? [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2]
          : [-115.01, 49.61];
        const map = new maplibre.Map({
          container: ref.current,
          style: {
            version: 8,
            sources: {
              osm: {
                type: "raster",
                tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                tileSize: 256,
                attribution: "OpenStreetMap",
              },
            },
            layers: [{ id: "osm", type: "raster", source: "osm" }],
          },
          center,
          zoom: 10,
        });
        mapRef.current = map;
        map.addControl(new maplibre.NavigationControl({ visualizePitch: false }), "top-right");
        map.on("load", () => {
          if (!map.getSource("aoi")) {
            map.addSource("aoi", { type: "geojson", data: aoi.geojson });
            map.addLayer({
              id: "aoi-fill",
              type: "fill",
              source: "aoi",
              paint: { "fill-color": "#8fbc8f", "fill-opacity": 0.16 },
            });
            map.addLayer({
              id: "aoi-outline",
              type: "line",
              source: "aoi",
              paint: { "line-color": "#d6a75b", "line-width": 3 },
            });
          }
          if (bbox) {
            map.fitBounds(
              [
                [bbox[0], bbox[1]],
                [bbox[2], bbox[3]],
              ],
              { padding: 28, duration: 0 },
            );
          }
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : "Map failed to load");
      }
    }
    setup();
    return () => {
      cancelled = true;
      const maybeMap = mapRef.current as { remove?: () => void } | null;
      maybeMap?.remove?.();
      mapRef.current = null;
    };
  }, [aoi]);

  return (
    <div className="relative h-[360px] overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--panel-strong)]">
      <div ref={ref} className="h-full w-full" />
      {error ? (
        <div className="absolute inset-x-3 bottom-3 rounded-md bg-[#2a1714] px-3 py-2 text-sm text-[#ffd2c9]">
          {error}
        </div>
      ) : null}
    </div>
  );
}
