import { Select } from "antd";
import { useTeam } from "../auth/TeamContext";

export default function TeamSwitcher() {
  const { teams, currentTeam, setCurrentTeam } = useTeam();

  if (teams.length === 0) {
    return null;
  }

  return (
    <Select
      value={currentTeam?.id}
      style={{ width: 180 }}
      options={teams.map((team) => ({ value: team.id, label: team.name }))}
      onChange={(value) => {
        const team = teams.find((t) => t.id === value);
        if (team) {
          setCurrentTeam(team);
        }
      }}
    />
  );
}
