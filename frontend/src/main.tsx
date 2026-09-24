import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router";
import { App as AntApp, ConfigProvider } from "antd";
import App from "./App";
import { themeConfig } from "./theme";
import "./index.css";

const queryClient = new QueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ConfigProvider theme={themeConfig}>
          <AntApp>
            <App />
          </AntApp>
        </ConfigProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
