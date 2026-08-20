"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  createMountain,
  deleteMountain,
  getMountain,
  listMountains,
  type MountainId,
  type MountainList,
  type MountainSummary,
} from "@/lib/twin";

const POLL_MS = 2000;

type Props = {
  mountain: MountainId;
  onSelect: (mountain: MountainId) => void;
};

const STAGE_ORDER = [
  "Receiving upload",
  "Preparing",
  "Reading elevation",
  "Building elevation image",
  "Rendering terrain tiles",
  "Rendering imagery tiles",
  "Writing analysis layers",
  "Building exposure",
  "Finishing",
];

/**
 * Choose which mountain the whole app is looking at, and upload new ones.
 *
 * Two things this deliberately makes visible rather than smoothing over:
 * an uploaded mountain carries none of the provenance the reviewed demo has, and
 * one without land cover cannot be assessed at all. Both are stated on the
 * mountain's own row, before anything is run against it.
 */
export function MountainPicker({ mountain, onSelect }: Props) {
  const [catalog, setCatalog] = useState<MountainList | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const pending = useRef<Set<string>>(new Set());

  const refresh = useCallback(async () => {
    try {
      setCatalog(await listMountains());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    listMountains()
      .then(setCatalog)
      .catch((caught) => setError(caught instanceof Error ? caught.message : String(caught)));
  }, []);

  // Poll only while something is actually building. A determinate stage list is
  // worth more than a spinner when a bake takes a couple of minutes.
  const building = (catalog?.mountains ?? []).filter(
    (item) => item.status === "validating" || item.status === "baking",
  );
  useEffect(() => {
    if (building.length === 0) return undefined;
    const timer = setInterval(() => {
      building.forEach(async (item) => {
        if (pending.current.has(item.id)) return;
        pending.current.add(item.id);
        try {
          const fresh = await getMountain(item.id);
          setCatalog((previous) =>
            previous === null
              ? previous
              : {
                  ...previous,
                  mountains: previous.mountains.map((row) => (row.id === fresh.id ? fresh : row)),
                },
          );
        } catch {
          // A poll that fails is not worth surfacing; the next tick retries.
        } finally {
          pending.current.delete(item.id);
        }
      });
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [building]);

  const upload = useCallback(
    async (form: HTMLFormElement) => {
      const data = new FormData(form);
      const dem = data.get("dem");
      if (!(dem instanceof File) || dem.size === 0) {
        setError("Choose an elevation raster to upload.");
        return;
      }
      const landcover = data.get("landcover");
      setUploading(true);
      setError(null);
      try {
        const created = await createMountain({
          name: String(data.get("name") ?? "").trim(),
          provider: String(data.get("provider") ?? "").trim(),
          citation: String(data.get("citation") ?? "").trim(),
          licence: String(data.get("licence") ?? "").trim(),
          elevationsAreMetres: data.get("elevations_are_metres") === "on",
          dem,
          landcover: landcover instanceof File && landcover.size > 0 ? landcover : null,
        });
        setCatalog((previous) =>
          previous === null ? previous : { ...previous, mountains: [...previous.mountains, created] },
        );
        setShowForm(false);
        form.reset();
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setUploading(false);
      }
    },
    [],
  );

  const remove = useCallback(
    async (id: string) => {
      try {
        await deleteMountain(id);
        if (mountain === id) onSelect(null);
        await refresh();
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    },
    [mountain, onSelect, refresh],
  );

  const uploads = catalog?.mountains ?? [];
  const full = catalog !== null && uploads.length >= catalog.max_mountains;

  return (
    <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
      <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
        Mountain
      </h3>

      <div className="flex flex-col gap-1">
        <MountainRow
          label="Mount Hosmer"
          detail="Reviewed demo bake · verified source manifest"
          selected={mountain === null}
          onSelect={() => onSelect(null)}
        />
        {uploads.map((item) => (
          <UploadedRow
            key={item.id}
            item={item}
            selected={mountain === item.id}
            onSelect={() => item.status === "ready" && onSelect(item.id)}
            onDelete={() => remove(item.id)}
          />
        ))}
      </div>

      {catalog !== null && !catalog.upload_available ? (
        <p className="mt-3 text-[11px] leading-relaxed text-[var(--muted)]">
          {catalog.upload_unavailable_reason}
        </p>
      ) : (
        <>
          <button
            type="button"
            className="mt-3 w-full rounded border border-[var(--border)] px-2 py-1.5 text-xs text-[var(--foreground)] disabled:opacity-50"
            disabled={full || uploading}
            onClick={() => setShowForm((open) => !open)}
          >
            {full
              ? `Limit reached (${catalog?.max_mountains}) — delete one to add another`
              : showForm
                ? "Cancel"
                : "Upload a mountain"}
          </button>
          {showForm && (
            <UploadForm uploading={uploading} onSubmit={upload} maxBytes={catalog?.max_upload_bytes} />
          )}
        </>
      )}

      {error && (
        <p className="mt-2 rounded border border-[var(--accent-2)] px-2 py-1 text-[11px] text-[var(--accent-2)]">
          {error}
        </p>
      )}
    </section>
  );
}

function MountainRow({
  label,
  detail,
  selected,
  onSelect,
}: {
  label: string;
  detail: string;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`rounded border px-2 py-1.5 text-left text-xs ${
        selected
          ? "border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--foreground)]"
          : "border-[var(--border)] text-[var(--muted)]"
      }`}
    >
      <span className="block font-semibold text-[var(--foreground)]">{label}</span>
      <span className="block text-[11px] leading-relaxed">{detail}</span>
    </button>
  );
}

function UploadedRow({
  item,
  selected,
  onSelect,
  onDelete,
}: {
  item: MountainSummary;
  selected: boolean;
  onSelect: () => void;
  onDelete: () => void;
}) {
  const stageIndex = item.stage ? STAGE_ORDER.indexOf(item.stage) : -1;
  const progress =
    stageIndex >= 0 ? ` · step ${stageIndex + 1} of ${STAGE_ORDER.length}: ${item.stage}` : "";

  const detail =
    item.status === "ready"
      ? item.assessable
        ? "Uploaded · unverified source · assessable"
        : "Uploaded · unverified source · 3D only, cannot be assessed"
      : item.status === "failed"
        ? `Failed: ${item.error ?? "unknown error"}`
        : `${item.status === "validating" ? "Checking the raster" : "Baking"}${progress}`;

  return (
    <div
      className={`rounded border px-2 py-1.5 text-xs ${
        selected
          ? "border-[var(--accent)] bg-[var(--accent)]/10"
          : "border-[var(--border)]"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <button
          type="button"
          onClick={onSelect}
          disabled={item.status !== "ready"}
          className="flex-1 text-left disabled:cursor-default"
        >
          <span className="block font-semibold text-[var(--foreground)]">{item.name}</span>
          <span className="block text-[11px] leading-relaxed text-[var(--muted)]">{detail}</span>
        </button>
        <button
          type="button"
          onClick={onDelete}
          disabled={item.status === "baking"}
          className="rounded border border-[var(--border)] px-1.5 py-0.5 text-[10px] text-[var(--muted)] disabled:opacity-40"
          aria-label={`Delete ${item.name}`}
        >
          Delete
        </button>
      </div>
      {item.status === "ready" && item.warnings.length > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer text-[11px] text-[var(--accent-2)]">
            {item.warnings.length} caveat{item.warnings.length === 1 ? "" : "s"}
          </summary>
          <ul className="mt-1 list-disc space-y-1 pl-4 text-[11px] leading-relaxed text-[var(--muted)]">
            {item.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function UploadForm({
  uploading,
  onSubmit,
  maxBytes,
}: {
  uploading: boolean;
  onSubmit: (form: HTMLFormElement) => void;
  maxBytes?: number;
}) {
  const field = "w-full rounded border border-[var(--border)] bg-[var(--background)] px-2 py-1 text-xs";
  return (
    <form
      className="mt-3 flex flex-col gap-2 border-t border-[var(--border)] pt-3"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit(event.currentTarget);
      }}
    >
      <label className="flex flex-col gap-1 text-[11px] text-[var(--muted)]">
        Elevation raster (GeoTIFF, single band)
        <input type="file" name="dem" accept=".tif,.tiff" required className={field} />
      </label>
      <label className="flex flex-col gap-1 text-[11px] text-[var(--muted)]">
        Land cover raster (optional)
        <input type="file" name="landcover" accept=".tif,.tiff" className={field} />
      </label>
      {/* Not a nicety: forest_mask is a required layer of the release model, so a
          DEM on its own produces a mountain that renders and assesses nothing. */}
      <p className="text-[11px] leading-relaxed text-[var(--muted)]">
        Without land cover the mountain renders in 3D but cannot be assessed — the release model
        requires forest cover, and missing data is never treated as &ldquo;no forest&rdquo;.
      </p>

      <label className="flex flex-col gap-1 text-[11px] text-[var(--muted)]">
        Name
        <input type="text" name="name" required maxLength={160} className={field} />
      </label>
      {/* All three are required by SourceStatement; a pack cannot be built without
          them, so there is no point accepting an upload that omits any. */}
      <label className="flex flex-col gap-1 text-[11px] text-[var(--muted)]">
        Data provider
        <input type="text" name="provider" required maxLength={200} className={field} />
      </label>
      <label className="flex flex-col gap-1 text-[11px] text-[var(--muted)]">
        Citation
        <input type="text" name="citation" required maxLength={500} className={field} />
      </label>
      <label className="flex flex-col gap-1 text-[11px] text-[var(--muted)]">
        Licence
        <input type="text" name="licence" required maxLength={200} className={field} />
      </label>

      <label className="flex items-start gap-2 text-[11px] leading-relaxed text-[var(--muted)]">
        <input type="checkbox" name="elevations_are_metres" required className="mt-0.5" />
        <span>
          I confirm the elevation values are in <strong>metres</strong>. Values in feet are never
          converted and would inflate every slope and runout distance by about 3.3×.
        </span>
      </label>

      <button
        type="submit"
        disabled={uploading}
        className="rounded border border-[var(--accent)] px-2 py-1.5 text-xs font-semibold text-[var(--accent)] disabled:opacity-50"
      >
        {uploading ? "Uploading…" : "Upload and bake"}
      </button>
      {maxBytes ? (
        <p className="text-[11px] text-[var(--muted)]">
          Maximum {Math.round(maxBytes / (1024 * 1024))} MB per file.
        </p>
      ) : null}
    </form>
  );
}
