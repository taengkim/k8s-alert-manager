import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useSearchParams } from "react-router";
import { useAuth } from "./AuthProvider";
import type { TeamSummary } from "../api/types";

const STORAGE_KEY = "kam.team";

interface TeamContextValue {
  teams: TeamSummary[];
  currentTeam: TeamSummary | null;
  setCurrentTeam: (team: TeamSummary) => void;
}

const TeamContext = createContext<TeamContextValue | undefined>(undefined);

export function TeamProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const teams = useMemo(() => user?.teams ?? [], [user]);
  const [searchParams, setSearchParams] = useSearchParams();
  const [currentTeamId, setCurrentTeamId] = useState<number | null>(null);

  // Resolve the active team on load / whenever the membership list changes:
  // prefer the `team` URL param, then localStorage, then the first team.
  useEffect(() => {
    if (teams.length === 0) {
      setCurrentTeamId(null);
      return;
    }

    const fromUrl = searchParams.get("team");
    const fromStorage = localStorage.getItem(STORAGE_KEY);
    const candidateId = Number(fromUrl ?? fromStorage);
    const match = teams.find((team) => team.id === candidateId);

    setCurrentTeamId(match ? match.id : teams[0].id);
    // Intentionally re-runs only when the team list changes, not on every
    // URL/localStorage change, so switching teams doesn't get overridden.
  }, [teams]);

  const currentTeam = useMemo(
    () => teams.find((team) => team.id === currentTeamId) ?? null,
    [teams, currentTeamId],
  );

  const setCurrentTeam = (team: TeamSummary) => {
    setCurrentTeamId(team.id);
    localStorage.setItem(STORAGE_KEY, String(team.id));
    const next = new URLSearchParams(searchParams);
    next.set("team", String(team.id));
    setSearchParams(next, { replace: true });
  };

  return (
    <TeamContext.Provider value={{ teams, currentTeam, setCurrentTeam }}>
      {children}
    </TeamContext.Provider>
  );
}

export function useTeam(): TeamContextValue {
  const ctx = useContext(TeamContext);
  if (!ctx) {
    throw new Error("useTeam must be used within a TeamProvider");
  }
  return ctx;
}
