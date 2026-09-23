import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { listClusters } from "../api/admin";
import type { Cluster } from "../api/types";

const STORAGE_KEY = "kam.clusters";
const URL_PARAM = "clusters";

interface ClusterFilterContextValue {
  /** Every cluster the current user can see (admin or not). */
  clusters: Cluster[];
  isLoading: boolean;
  /** Empty means "All" -- every enabled cluster, unfiltered. */
  selectedIds: number[];
  setSelectedIds: (ids: number[]) => void;
  /** `selectedIds` resolved against the currently loaded cluster list:
   * every *enabled* cluster when selectedIds is empty ("All"), otherwise
   * just the selected ones (regardless of enabled -- an explicit pick
   * always applies). This is what read views should actually query. */
  activeClusters: Cluster[];
}

function parseIds(raw: string | null): number[] {
  if (!raw) return [];
  return raw
    .split(",")
    .map((s) => Number(s))
    .filter((n) => Number.isFinite(n));
}

const ClusterFilterContext = createContext<ClusterFilterContextValue | undefined>(undefined);

export function ClusterFilterProvider({ children }: { children: ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const clustersQuery = useQuery({ queryKey: ["clusters"], queryFn: listClusters });
  const clusters = useMemo(() => clustersQuery.data ?? [], [clustersQuery.data]);

  // Resolved once at mount, same precedence as TeamContext: URL param, then
  // localStorage, then "All" (empty). Not re-derived on every render so a
  // user's explicit "All" choice isn't overridden by a stale URL/storage
  // value re-appearing.
  const [selectedIds, setSelectedIdsState] = useState<number[]>(() => {
    const fromUrl = searchParams.get(URL_PARAM);
    if (fromUrl !== null) return parseIds(fromUrl);
    try {
      const fromStorage = localStorage.getItem(STORAGE_KEY);
      if (fromStorage) {
        const parsed = JSON.parse(fromStorage);
        if (Array.isArray(parsed)) return parsed.filter((n) => typeof n === "number");
      }
    } catch {
      // Corrupt localStorage value -- fall through to "All".
    }
    return [];
  });

  const setSelectedIds = (ids: number[]) => {
    setSelectedIdsState(ids);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
    const next = new URLSearchParams(searchParams);
    if (ids.length > 0) {
      next.set(URL_PARAM, ids.join(","));
    } else {
      next.delete(URL_PARAM);
    }
    setSearchParams(next, { replace: true });
  };

  const activeClusters = useMemo(() => {
    if (selectedIds.length === 0) return clusters.filter((c) => c.enabled);
    const wanted = new Set(selectedIds);
    return clusters.filter((c) => wanted.has(c.id));
  }, [clusters, selectedIds]);

  return (
    <ClusterFilterContext.Provider
      value={{
        clusters,
        isLoading: clustersQuery.isLoading,
        selectedIds,
        setSelectedIds,
        activeClusters,
      }}
    >
      {children}
    </ClusterFilterContext.Provider>
  );
}

export function useClusterFilter(): ClusterFilterContextValue {
  const ctx = useContext(ClusterFilterContext);
  if (!ctx) {
    throw new Error("useClusterFilter must be used within a ClusterFilterProvider");
  }
  return ctx;
}
