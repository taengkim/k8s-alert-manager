import { useQuery } from "@tanstack/react-query";
import { listClusters } from "./admin";
import type { Cluster } from "./types";

interface UseDefaultClusterResult {
  cluster: Cluster | null;
  isLoading: boolean;
}

/**
 * This phase only ever has one usable cluster in practice, so rule calls
 * just use the first enabled one. A header ClusterFilter for real
 * multi-cluster selection is Phase 11.
 */
export function useDefaultCluster(): UseDefaultClusterResult {
  const query = useQuery({ queryKey: ["clusters"], queryFn: listClusters });
  const cluster = query.data?.find((c) => c.enabled) ?? null;
  return { cluster, isLoading: query.isLoading };
}
