import { useMemo } from "react";
import { Alert, Layout, Menu } from "antd";
import { Link, Navigate, Outlet, Route, Routes, useLocation } from "react-router";
import { AuthProvider, useAuth } from "./auth/AuthProvider";
import RequireAuth from "./auth/RequireAuth";
import { TeamProvider } from "./auth/TeamContext";
import { ClusterFilterProvider, useClusterFilter } from "./auth/ClusterFilterContext";
import StatusStrip from "./components/StatusStrip";
import { monoFontFamily, palette } from "./theme";
import Alerts from "./pages/Alerts";
import AlertHistory from "./pages/AlertHistory";
import Channels from "./pages/Channels";
import Login from "./pages/Login";
import RouteEditor from "./pages/RouteEditor";
import RoutesPage from "./pages/Routes";
import RuleEditor from "./pages/RuleEditor";
import Rules from "./pages/Rules";
import Shares from "./pages/Shares";
import Silences from "./pages/Silences";
import Stats from "./pages/Stats";
import TeamSettings from "./pages/TeamSettings";
import Templates from "./pages/Templates";
import TemplateEditor from "./pages/TemplateEditor";
import Admin from "./pages/Admin";

const { Sider, Content } = Layout;

const sections = [
  { key: "alerts", label: "Alerts", path: "/alerts" },
  { key: "rules", label: "Rules", path: "/rules" },
  { key: "silences", label: "Silences", path: "/silences" },
  { key: "channels", label: "Channels", path: "/channels" },
  { key: "templates", label: "템플릿", path: "/templates" },
  { key: "routes", label: "Routes", path: "/routes" },
  { key: "shares", label: "공유", path: "/shares" },
  { key: "stats", label: "통계", path: "/stats" },
];

// Path -> section title for the StatusStrip's left-hand label. Kept next to
// `sections`/selectedKeys below since both are derived from the same route
// table -- most specific path first (see selectedKeys' own note on why
// "/alerts/history" has to win over "/alerts").
const TITLE_BY_PATH: { path: string; title: string }[] = [
  { path: "/alerts/history", title: "알럿 이력" },
  ...sections.map((s) => ({ path: s.path, title: s.label })),
  { path: "/team", title: "팀 설정" },
  { path: "/admin", title: "관리자" },
];

function AppLayout() {
  const { user } = useAuth();
  const location = useLocation();
  const { isError: clustersError } = useClusterFilter();

  const menuItems = useMemo(() => {
    const items = sections.map((section) => ({
      key: section.path,
      label: <Link to={section.path}>{section.label}</Link>,
    }));
    items.push({ key: "/alerts/history", label: <Link to="/alerts/history">알럿 이력</Link> });
    items.push({ key: "/team", label: <Link to="/team">팀 설정</Link> });
    if (user?.is_admin) {
      items.push({ key: "/admin", label: <Link to="/admin">관리자</Link> });
    }
    return items;
  }, [user]);

  const selectedKeys = useMemo(() => {
    // "/alerts/history" must be checked before "/alerts" -- both match a
    // startsWith test against that pathname, so the more specific one has
    // to come first or it never wins.
    const known = ["/alerts/history", ...sections.map((s) => s.path), "/team", "/admin"];
    const match = known.find((path) => location.pathname.startsWith(path));
    return match ? [match] : [];
  }, [location.pathname]);

  const sectionTitle = useMemo(() => {
    const match = TITLE_BY_PATH.find((entry) => location.pathname.startsWith(entry.path));
    return match?.title ?? "K8s Alert Manager";
  }, [location.pathname]);

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider className="kam-sider" style={{ borderRight: `1px solid ${palette.hairline}` }}>
        <div style={{ padding: "20px 20px 16px" }}>
          <div style={{ fontFamily: monoFontFamily, fontWeight: 600, fontSize: 18, color: palette.ink }}>
            KAM
          </div>
          <div style={{ fontSize: 12, color: palette.inkMuted, marginTop: 2 }}>Alert Manager</div>
        </div>
        <Menu mode="inline" items={menuItems} selectedKeys={selectedKeys} />
      </Sider>
      <Layout>
        <StatusStrip title={sectionTitle} />
        <Content style={{ padding: 20, background: palette.paper }}>
          {clustersError && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              message="클러스터 목록을 불러오지 못했습니다 — 표시된 목록이 불완전할 수 있습니다"
            />
          )}
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}

function AuthenticatedShell() {
  return (
    <TeamProvider>
      <ClusterFilterProvider>
        <AppLayout />
      </ClusterFilterProvider>
    </TeamProvider>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<RequireAuth />}>
          <Route element={<AuthenticatedShell />}>
            <Route path="/" element={<Navigate to="/alerts" replace />} />
            <Route path="/alerts" element={<Alerts />} />
            <Route path="/alerts/history" element={<AlertHistory />} />
            <Route path="/rules" element={<Rules />} />
            <Route path="/rules/new" element={<RuleEditor />} />
            <Route path="/rules/:slug/edit" element={<RuleEditor />} />
            <Route path="/silences" element={<Silences />} />
            <Route path="/channels" element={<Channels />} />
            <Route path="/templates" element={<Templates />} />
            <Route path="/templates/new" element={<TemplateEditor />} />
            <Route path="/templates/:id/edit" element={<TemplateEditor />} />
            <Route path="/routes" element={<RoutesPage />} />
            <Route path="/routes/new" element={<RouteEditor />} />
            <Route path="/routes/:id/edit" element={<RouteEditor />} />
            <Route path="/shares" element={<Shares />} />
            <Route path="/stats" element={<Stats />} />
            <Route path="/team" element={<TeamSettings />} />
            <Route path="/admin" element={<Admin />} />
          </Route>
        </Route>
      </Routes>
    </AuthProvider>
  );
}
