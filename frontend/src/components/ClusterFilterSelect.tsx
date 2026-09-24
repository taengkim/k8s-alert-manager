import { Select } from "antd";
import { useClusterFilter } from "../auth/ClusterFilterContext";
import { useI18n } from "../i18n";

/** Header-level multi-select: narrows every read view (Alerts, AlertHistory,
 * Silences, Rules) to the selected clusters. Empty selection means "All". */
export default function ClusterFilterSelect() {
  const { t } = useI18n();
  const { clusters, selectedIds, setSelectedIds } = useClusterFilter();

  if (clusters.length <= 1) {
    return null;
  }

  return (
    <Select
      mode="multiple"
      allowClear
      placeholder={t("common.allClusters")}
      style={{ minWidth: 200, maxWidth: 320 }}
      value={selectedIds}
      onChange={setSelectedIds}
      maxTagCount="responsive"
      options={clusters.map((c) => ({ value: c.id, label: c.display_name }))}
    />
  );
}
