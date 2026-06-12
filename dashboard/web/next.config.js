/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    // single user-facing port (8888); FastAPI bridge stays internal on 8801
    return [{ source: "/api/:path*", destination: "http://127.0.0.1:8801/api/:path*" }];
  },
};
module.exports = nextConfig;
