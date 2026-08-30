/**
 * Next.js configuration.
 *
 * Intentionally minimal. Two things are worth calling out:
 *
 * 1. `reactStrictMode` is on, so effects double-invoke in development. That is
 *    desirable here: it surfaces any accidental "fetch in an effect without
 *    cleanup" bug during development rather than in front of a reviewer.
 *
 * 2. There is no `rewrites()` proxy to the backend. The browser talks to
 *    FastAPI directly at NEXT_PUBLIC_API_BASE_URL, and FastAPI allows the
 *    origin via CORS. One hop, one place to look when something 404s.
 */
/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
};

export default nextConfig;
