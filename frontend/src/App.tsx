import { useMemo } from "react";
import { Dropdown, Layout, Menu } from "antd";
import { Link, Navigate, Outlet, Route, Routes, useLocation, useNavigate } from "react-router";
import { AuthProvider, useAuth } from "./auth/AuthProvider";
import RequireAuth from "./auth/RequireAuth";
import { TeamProvider } from "./auth/TeamContext";
import TeamSwitcher from "./components/TeamSwitcher";
import Login from "./pages/Login";
import Placeholder from "./pages/Placeholder";
import TeamSettings from "./pages/TeamSettings";
import Admin from "./pages/Admin";

const { Header, Sider, Content } = Layout;

const sections = [
  { key: "alerts", label: "Alerts", path: "/alerts" },
  { key: "rules", label: "Rules", path: "/rules" },
  { key: "silences", label: "Silences", path: "/silences" },
  { key: "channels", label: "Channels", path: "/channels" },
  { key: "routes", label: "Routes", path: "/routes" },
];

function AppLayout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const menuItems = useMemo(() => {
    const items = sections.map((section) => ({
      key: section.path,
      label: <Link to={section.path}>{section.label}</Link>,
    }));
    items.push({ key: "/team", label: <Link to="/team">팀 설정</Link> });
    if (user?.is_admin) {
      items.push({ key: "/admin", label: <Link to="/admin">관리자</Link> });
    }
    return items;
  }, [user]);

  const selectedKeys = useMemo(() => {
    const known = [...sections.map((s) => s.path), "/team", "/admin"];
    const match = known.find((path) => location.pathname.startsWith(path));
    return match ? [match] : [];
  }, [location.pathname]);

  const handleLogout = async () => {
    await logout();
    navigate("/login", { replace: true });
  };

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider>
        <Menu theme="dark" mode="inline" items={menuItems} selectedKeys={selectedKeys} />
      </Sider>
      <Layout>
        <Header
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            color: "white",
            fontSize: 18,
          }}
        >
          <span>K8s Alert Manager</span>
          <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
            <TeamSwitcher />
            <Dropdown menu={{ items: [{ key: "logout", label: "로그아웃", onClick: handleLogout }] }}>
              <span style={{ cursor: "pointer", color: "white" }}>{user?.display_name} ▾</span>
            </Dropdown>
          </div>
        </Header>
        <Content style={{ padding: 24 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}

function AuthenticatedShell() {
  return (
    <TeamProvider>
      <AppLayout />
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
            {sections.map((section) => (
              <Route
                key={section.key}
                path={section.path}
                element={<Placeholder title={section.label} />}
              />
            ))}
            <Route path="/team" element={<TeamSettings />} />
            <Route path="/admin" element={<Admin />} />
          </Route>
        </Route>
      </Routes>
    </AuthProvider>
  );
}
