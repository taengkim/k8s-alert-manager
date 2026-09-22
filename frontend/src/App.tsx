import { Layout, Menu } from "antd";
import { Link, Route, Routes } from "react-router";
import Placeholder from "./pages/Placeholder";

const { Header, Sider, Content } = Layout;

const sections = [
  { key: "alerts", label: "Alerts", path: "/alerts" },
  { key: "rules", label: "Rules", path: "/rules" },
  { key: "silences", label: "Silences", path: "/silences" },
  { key: "channels", label: "Channels", path: "/channels" },
  { key: "routes", label: "Routes", path: "/routes" },
];

export default function App() {
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider>
        <Menu
          theme="dark"
          mode="inline"
          items={sections.map((section) => ({
            key: section.key,
            label: <Link to={section.path}>{section.label}</Link>,
          }))}
        />
      </Sider>
      <Layout>
        <Header style={{ color: "white", fontSize: 18 }}>
          K8s Alert Manager
        </Header>
        <Content style={{ padding: 24 }}>
          <Routes>
            {sections.map((section) => (
              <Route
                key={section.key}
                path={section.path}
                element={<Placeholder title={section.label} />}
              />
            ))}
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}
