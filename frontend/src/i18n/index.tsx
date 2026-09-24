import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { ko } from "./ko";
import { en } from "./en";

export type Lang = "ko" | "en";
export type TranslationKey = keyof typeof ko;

const STORAGE_KEY = "kam.lang";

const DICTS: Record<Lang, Record<TranslationKey, string>> = { ko, en };

interface I18nValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  /** Look up a key in the active language; `{name}` placeholders are
   * replaced from params. Unknown keys fall back to Korean, then to the
   * key itself -- rendering something beats rendering nothing. */
  t: (key: TranslationKey, params?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nValue | undefined>(undefined);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === "en" || stored === "ko" ? stored : "ko";
  });

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    localStorage.setItem(STORAGE_KEY, next);
  }, []);

  const t = useCallback(
    (key: TranslationKey, params?: Record<string, string | number>): string => {
      let text: string = DICTS[lang][key] ?? ko[key] ?? key;
      if (params) {
        for (const [name, value] of Object.entries(params)) {
          text = text.replaceAll(`{${name}}`, String(value));
        }
      }
      return text;
    },
    [lang],
  );

  const value = useMemo(() => ({ lang, setLang, t }), [lang, setLang, t]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("useI18n must be used within I18nProvider");
  return ctx;
}
