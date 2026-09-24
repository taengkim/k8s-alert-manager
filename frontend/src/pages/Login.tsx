import { useState } from "react";
import { Alert, Button, Card, Form, Input } from "antd";
import { useLocation, useNavigate } from "react-router";
import { useAuth } from "../auth/AuthProvider";
import { ApiError } from "../api/client";
import { useI18n } from "../i18n";

interface LoginFormValues {
  username: string;
  password: string;
}

interface LocationState {
  from?: { pathname: string };
}

export default function Login() {
  const { t } = useI18n();
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const from = (location.state as LocationState | null)?.from?.pathname ?? "/alerts";

  const handleFinish = async (values: LoginFormValues) => {
    setSubmitting(true);
    setError(null);
    try {
      await login(values.username, values.password);
      navigate(from, { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 401 || err.status === 422) {
          setError(t("login.errorInvalidCredentials"));
        } else if (err.status === 403) {
          setError(t("login.errorAccountDisabled"));
        } else if (err.status === 503) {
          setError(t("login.errorAuthServerUnavailable"));
        } else {
          setError(t("login.errorGeneric"));
        }
      } else {
        setError(t("login.errorGeneric"));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        height: "100vh",
        background: "#f0f2f5",
      }}
    >
      <Card title={t("app.title")} style={{ width: 360 }}>
        <Form<LoginFormValues> layout="vertical" onFinish={handleFinish} disabled={submitting}>
          {error && (
            <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />
          )}
          <Form.Item
            name="username"
            label={t("login.usernameLabel")}
            rules={[{ required: true, message: t("login.usernameRequired") }]}
          >
            <Input autoFocus autoComplete="username" />
          </Form.Item>
          <Form.Item
            name="password"
            label={t("login.passwordLabel")}
            rules={[{ required: true, message: t("login.passwordRequired") }]}
          >
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block loading={submitting}>
              {t("login.submit")}
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
