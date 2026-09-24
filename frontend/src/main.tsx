import { StrictMode, useEffect, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router";
import { App as AntApp, ConfigProvider } from "antd";
import koKR from "antd/locale/ko_KR";
import enUS from "antd/locale/en_US";
import dayjs from "dayjs";
import "dayjs/locale/ko";
import App from "./App";
import { themeConfig } from "./theme";
import { I18nProvider, useI18n } from "./i18n";
import "./index.css";

const queryClient = new QueryClient();

/** Inside I18nProvider: keeps antd's built-in strings (pagination, pickers,
 * confirm buttons) and dayjs's relative times on the selected language. */
function LocalizedConfig({ children }: { children: ReactNode }) {
  const { lang } = useI18n();
  useEffect(() => {
    dayjs.locale(lang);
  }, [lang]);
  return (
    <ConfigProvider theme={themeConfig} locale={lang === "ko" ? koKR : enUS}>
      {children}
    </ConfigProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <I18nProvider>
          <LocalizedConfig>
            <AntApp>
              <App />
            </AntApp>
          </LocalizedConfig>
        </I18nProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
