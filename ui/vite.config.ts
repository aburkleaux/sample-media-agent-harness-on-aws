import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import dotenv from "dotenv";
import agentcoreProxy from "./agentcore-proxy";

dotenv.config();

export default defineConfig({
  plugins: [react(), agentcoreProxy()],
  server: {
    port: 3000,
  },
});
