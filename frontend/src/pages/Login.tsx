import { useState } from "react";
import { Alert, Button, Card, Form, Input } from "antd";
import { useLocation, useNavigate } from "react-router";
import { useAuth } from "../auth/AuthProvider";
import { ApiError } from "../api/client";

interface LoginFormValues {
  username: string;
  password: string;
}

interface LocationState {
  from?: { pathname: string };
}

export default function Login() {
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
          setError("아이디 또는 비밀번호가 올바르지 않습니다");
        } else if (err.status === 403) {
          setError("비활성화된 계정입니다");
        } else {
          setError(err.detail || "로그인에 실패했습니다");
        }
      } else {
        setError("로그인에 실패했습니다");
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
      <Card title="K8s Alert Manager" style={{ width: 360 }}>
        <Form<LoginFormValues> layout="vertical" onFinish={handleFinish} disabled={submitting}>
          {error && (
            <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />
          )}
          <Form.Item
            name="username"
            label="아이디"
            rules={[{ required: true, message: "아이디를 입력하세요" }]}
          >
            <Input autoFocus autoComplete="username" />
          </Form.Item>
          <Form.Item
            name="password"
            label="비밀번호"
            rules={[{ required: true, message: "비밀번호를 입력하세요" }]}
          >
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block loading={submitting}>
              로그인
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
