export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Fleet-ID, X-Build-Num, X-User-Sub, X-User-Email, ngrok-skip-browser-warning",
      "Access-Control-Max-Age": "86400",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    if (!globalThis.__WIFEY_TOKEN_CACHE) {
      globalThis.__WIFEY_TOKEN_CACHE = new Map();
    }
    const tokenCache = globalThis.__WIFEY_TOKEN_CACHE;

    function normalizeEmailKey(email) {
      const clean = String(email || "").trim().toLowerCase();
      if (!clean) return "";
      return "email_" + clean.replace(/[^a-z0-9._-]/g, "_");
    }

    function decodeJwtPayload(token) {
      try {
        const parts = token.split(".");
        if (parts.length < 2) return null;
        let b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
        while (b64.length % 4 !== 0) b64 += "=";
        const jsonStr = atob(b64);
        return JSON.parse(jsonStr);
      } catch (_) {
        return null;
      }
    }

    async function fetchTokenInfoWithRetry(token, retries = 2) {
      for (let attempt = 0; attempt <= retries; attempt++) {
        try {
          const resp = await fetch(`https://oauth2.googleapis.com/tokeninfo?id_token=${encodeURIComponent(token)}`);
          if (resp.ok) return await resp.json();
          if (resp.status >= 400 && resp.status < 500) break;
        } catch (_) { }
        if (attempt < retries) {
          await new Promise((r) => setTimeout(r, 150 * (attempt + 1)));
        }
      }
      return null;
    }

    async function getAuthContext(req) {
      const authHeader = req.headers.get("Authorization") || "";
      const headerSub = (req.headers.get("X-User-Sub") || "").trim();
      const headerEmail = (req.headers.get("X-User-Email") || "").trim().toLowerCase();

      if (!authHeader.startsWith("Bearer ")) {
        if (headerSub || headerEmail) {
          return {
            sub: headerSub,
            email: headerEmail,
            emailKey: normalizeEmailKey(headerEmail),
          };
        }
        return null;
      }

      const token = authHeader.slice(7).trim();
      if (!token || token === "local_mode_token") {
        if (headerSub || headerEmail) {
          return {
            sub: headerSub || "local_workspace",
            email: headerEmail,
            emailKey: normalizeEmailKey(headerEmail),
          };
        }
        return null;
      }

      const nowSec = Math.floor(Date.now() / 1000);
      const cached = tokenCache.get(token);
      if (cached && cached.expiresAt > nowSec) {
        return cached;
      }

      const data = await fetchTokenInfoWithRetry(token, 2);
      if (data && data.sub) {
        const ctx = {
          sub: String(data.sub),
          email: String(data.email || headerEmail || "").trim().toLowerCase(),
          emailKey: normalizeEmailKey(data.email || headerEmail || ""),
          expiresAt: Math.max(nowSec + 300, parseInt(data.exp || nowSec + 1800, 10)),
        };
        if (tokenCache.size > 500) tokenCache.clear();
        tokenCache.set(token, ctx);
        return ctx;
      }

      const jwt = decodeJwtPayload(token);
      if (jwt && jwt.sub) {
        const ctx = {
          sub: String(jwt.sub),
          email: String(jwt.email || headerEmail || "").trim().toLowerCase(),
          emailKey: normalizeEmailKey(jwt.email || headerEmail || ""),
          expiresAt: nowSec + 300,
        };
        tokenCache.set(token, ctx);
        return ctx;
      }

      if (headerSub || headerEmail) {
        return {
          sub: headerSub,
          email: headerEmail,
          emailKey: normalizeEmailKey(headerEmail),
        };
      }
      return null;
    }

    function isAuthorizedForSlot(auth, key) {
      // Collaboration mailboxes (users/email_*_collab/wifey_db.json) are shared exchange slots
      // so any signed-in or identified Studio instance can post requests/grants/mutations
      if (key.includes("_collab/wifey_db.json")) {
        return true;
      }
      if (!auth) return false;
      if (auth.sub && key.includes(`users/${auth.sub}/`)) return true;
      if (auth.emailKey && key.includes(`users/${auth.emailKey}/`)) return true;
      return false;
    }

    try {
      if (url.pathname === "/api/health" || url.pathname === "/health") {
        return new Response(JSON.stringify({ ok: true, ts: Date.now() }), {
          status: 200,
          headers: { ...corsHeaders, "Content-Type": "application/json", "Cache-Control": "no-store" },
        });
      }

      if (url.pathname.endsWith("/wifey_db.json")) {
        const key = decodeURIComponent(url.pathname.replace(/^\/+/, ""));

        if (request.method === "GET") {
          const object = await env.FIRMWARE_BUCKET.get(key);
          if (!object) {
            return new Response(JSON.stringify({ error: "Not found" }), {
              status: 404,
              headers: { ...corsHeaders, "Content-Type": "application/json", "Cache-Control": "no-store" },
            });
          }
          return new Response(object.body, {
            status: 200,
            headers: {
              ...corsHeaders,
              "Content-Type": "application/json",
              "Cache-Control": "no-store, no-cache, must-revalidate",
            },
          });
        }

        if (request.method === "PUT" || request.method === "POST") {
          const auth = await getAuthContext(request);
          if (!isAuthorizedForSlot(auth, key)) {
            return new Response(JSON.stringify({ error: "Unauthorized slot access" }), {
              status: 401,
              headers: { ...corsHeaders, "Content-Type": "application/json" },
            });
          }
          const bodyBuffer = await request.arrayBuffer();
          await env.FIRMWARE_BUCKET.put(key, bodyBuffer, {
            httpMetadata: { contentType: "application/json", cacheControl: "no-store" },
          });
          return new Response(JSON.stringify({ success: true, key }), {
            status: 200,
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          });
        }
      }

      if (url.pathname.endsWith("/api/firmware/upload") && request.method === "POST") {
        const auth = await getAuthContext(request);
        if (!auth || (!auth.sub && !auth.emailKey)) {
          return new Response(JSON.stringify({ error: "Unauthorized" }), {
            status: 401,
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          });
        }

        const userSlot = auth.sub || auth.emailKey;
        const rawFleet = request.headers.get("X-Fleet-ID") || "DefaultFleet";
        const safeFleet = rawFleet.replace(/[^A-Za-z0-9_.-]/g, "_") || "DefaultFleet";
        const buildNum = String(parseInt(request.headers.get("X-Build-Num") || "100", 10) || 100);

        const formData = await request.formData();
        const file = formData.get("file");
        if (!file) {
          return new Response(JSON.stringify({ error: "No file provided" }), {
            status: 400,
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          });
        }

        const fileBuffer = await file.arrayBuffer();
        if (!fileBuffer || fileBuffer.byteLength < 4096) {
          return new Response(JSON.stringify({ error: "Invalid or truncated firmware binary" }), {
            status: 400,
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          });
        }

        const objectKey = `users/${userSlot}/${safeFleet}/firmware_b${buildNum}.bin`;
        const fileUrl = `${url.origin}/${objectKey}`;

        await env.FIRMWARE_BUCKET.put(objectKey, fileBuffer, {
          httpMetadata: {
            contentType: "application/octet-stream",
            cacheControl: "public, max-age=31536000, immutable",
          },
          customMetadata: {
            fleet: safeFleet,
            build: buildNum,
            size: String(fileBuffer.byteLength),
            uploadedAt: String(Date.now()),
          },
        });

        const manifestKey = `users/${userSlot}/${safeFleet}/manifest.json`;
        const manifestData = JSON.stringify({
          target_build: parseInt(buildNum, 10),
          version: parseInt(buildNum, 10),
          firmware_url: fileUrl,
          download_url: fileUrl,
          size: fileBuffer.byteLength,
          updated_at: Date.now(),
        });

        await env.FIRMWARE_BUCKET.put(manifestKey, manifestData, {
          httpMetadata: {
            contentType: "application/json",
            cacheControl: "no-store, no-cache, must-revalidate",
          },
        });

        return new Response(
          JSON.stringify({
            success: true,
            download_url: fileUrl,
            url: fileUrl,
            size: fileBuffer.byteLength,
            target_build: parseInt(buildNum, 10),
          }),
          {
            status: 200,
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          }
        );
      }

      if (request.method === "GET" || request.method === "HEAD") {
        const objectKey = decodeURIComponent(url.pathname.replace(/^\/+/, ""));
        if (!objectKey) {
          return new Response("WiFeY Gateway Online", { status: 200, headers: corsHeaders });
        }

        const object = await env.FIRMWARE_BUCKET.get(objectKey);
        if (object) {
          const headers = new Headers(corsHeaders);
          object.writeHttpMetadata(headers);
          if (!headers.has("Content-Type")) {
            headers.set(
              "Content-Type",
              objectKey.endsWith(".json") ? "application/json" : "application/octet-stream"
            );
          }
          headers.set("Content-Length", object.size.toString());
          headers.set("Accept-Ranges", "bytes");
          if (object.httpEtag) {
            headers.set("ETag", object.httpEtag);
          }
          if (request.method === "HEAD") {
            return new Response(null, { status: 200, headers });
          }
          return new Response(object.body, { status: 200, headers });
        }
      }

      return new Response(JSON.stringify({ error: "Not found" }), {
        status: 404,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message || "Internal Worker Error" }), {
        status: 500,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
  },
};