import { joinURL } from "ufo";

export default defineEventHandler(async (event) => {
  const apiUrl = useRuntimeConfig().apiUrl; // 'http://localhost:9000'

  const target = joinURL(apiUrl, event.path);
  if (event.path === "/api/auth/oauth") {
    return sendRedirect(event, target); // redirect to BE for Oauth login
  }

  // Fork (MCP, docs/ai/PHASE3.md §4): hand the browser the authorize endpoint's redirect (to the consent page, or
  // back to the app) instead of following it here
  if (event.path.split("?")[0] === "/api/oauth/authorize") {
    return proxyRequest(event, target, { fetchOptions: { redirect: "manual" } });
  }

  return proxyRequest(event, target);
});
