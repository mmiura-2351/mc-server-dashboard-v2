// Fixture identities include a timestamp and counter to avoid collisions across runs.

import { type APIRequestContext, expect } from "@playwright/test";

const API_URL = process.env.MCD_E2E_API_URL ?? "http://127.0.0.1:8000";
const ADMIN_USERNAME = process.env.MCD_E2E_ADMIN_USERNAME ?? "e2e-admin";
const ADMIN_PASSWORD = process.env.MCD_E2E_ADMIN_PASSWORD ?? "E2eAdminPass!234";

export const adminCredentials = {
  username: ADMIN_USERNAME,
  password: ADMIN_PASSWORD,
};

const RUN_ID = `${Date.now().toString(36)}${Math.floor(Math.random() * 1e4)}`;
let counter = 0;

// Fixture passwords meet the API policy: at least 12 characters and three character classes.
export interface TestUser {
  username: string;
  email: string;
  password: string;
}

export function uniqueUser(prefix: string): TestUser {
  counter += 1;
  const tag = `${prefix}-${RUN_ID}-${counter}`;
  return {
    username: tag,
    email: `${tag}@example.com`,
    password: "E2eTestPass!234",
  };
}

export function uniqueName(prefix: string): string {
  counter += 1;
  return `${prefix}-${RUN_ID}-${counter}`;
}

export async function registerUser(
  request: APIRequestContext,
  user: TestUser,
): Promise<{ id: string }> {
  const res = await request.post(`${API_URL}/api/users`, { data: user });
  expect(res.status(), await res.text()).toBe(201);
  return res.json();
}

async function login(
  request: APIRequestContext,
  username: string,
  password: string,
): Promise<string> {
  const res = await request.post(`${API_URL}/api/auth/login`, {
    data: { username, password },
  });
  expect(res.ok(), await res.text()).toBeTruthy();
  const { access_token } = await res.json();
  return access_token;
}

export function adminToken(request: APIRequestContext): Promise<string> {
  return login(request, ADMIN_USERNAME, ADMIN_PASSWORD);
}

export async function provisionCommunity(
  request: APIRequestContext,
  ownerUserId: string,
  name: string,
): Promise<{ id: string }> {
  const token = await adminToken(request);
  const res = await request.post(`${API_URL}/api/communities`, {
    headers: { authorization: `Bearer ${token}` },
    data: { name, owner_user_id: ownerUserId },
  });
  expect(res.status(), await res.text()).toBe(201);
  return res.json();
}

// Use a custom role so assigning it cannot pass by re-granting the default Owner role.
export async function createRole(
  request: APIRequestContext,
  token: string,
  communityId: string,
  name: string,
  permissions: string[] = ["server:read"],
): Promise<{ id: string; name: string }> {
  const res = await request.post(
    `${API_URL}/api/communities/${communityId}/roles`,
    {
      headers: { authorization: `Bearer ${token}` },
      data: { name, permissions },
    },
  );
  expect(res.status(), await res.text()).toBe(201);
  return res.json();
}

export async function listServers(
  request: APIRequestContext,
  token: string,
  communityId: string,
): Promise<Array<{ id: string; name: string }>> {
  const res = await request.get(
    `${API_URL}/api/communities/${communityId}/servers`,
    { headers: { authorization: `Bearer ${token}` } },
  );
  expect(res.ok(), await res.text()).toBeTruthy();
  return res.json();
}

export async function listMembers(
  request: APIRequestContext,
  token: string,
  communityId: string,
): Promise<Array<{ user_id: string; username: string; role_names: string[] }>> {
  const res = await request.get(
    `${API_URL}/api/communities/${communityId}/members`,
    { headers: { authorization: `Bearer ${token}` } },
  );
  expect(res.ok(), await res.text()).toBeTruthy();
  return res.json();
}

export { login };
