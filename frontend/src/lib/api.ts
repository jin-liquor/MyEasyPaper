import axios, { type InternalAxiosRequestConfig } from "axios";
import { clearSession, getSession, isCurrentSession } from "./session";

const api = axios.create();
const requestSessions = new WeakMap<InternalAxiosRequestConfig, ReturnType<typeof getSession>>();

api.interceptors.request.use((config) => {
  const session = getSession();
  const publicAuth = config.url === "/api/auth/login" || config.url === "/api/auth/register";
  if (!publicAuth && session.token && isCurrentSession(session)) {
    config.headers.Authorization = `Bearer ${session.token}`;
    requestSessions.set(config, session);
  }
  return config;
}, undefined, { synchronous: true });

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const session = error.config && requestSessions.get(error.config);
    if (error.response?.status === 401 && session && isCurrentSession(session)) {
      clearSession();
    }
    return Promise.reject(error);
  },
);

export default api;
