export type UserRole = "SUPER_ADMIN" | "ADMIN" | "MASTER" | "DEALER" | "CLIENT";
export type UserStatus = "ACTIVE" | "BLOCKED" | "PENDING" | "CLOSED";
export type AccountType = "LIVE" | "DEMO";

export interface AuthUser {
  id: string;
  user_code: string;
  email: string;
  mobile: string;
  full_name: string;
  role: UserRole;
  status: UserStatus;
  is_demo: boolean;
  two_fa_enabled: boolean;
  must_change_password: boolean;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;
  user: AuthUser;
}

export interface ApiError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface ApiResponse<T> {
  success: boolean;
  data: T | null;
  message?: string | null;
  total?: number | null;
}

export interface ApiErrorResponse {
  error: ApiError;
}
