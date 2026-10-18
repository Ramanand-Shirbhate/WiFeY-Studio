export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "*",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    // --- AUTHENTICATION HELPER ---
    async function getUserId(req) {
      const authHeader = req.headers.get("Authorization");
      if (!authHeader || !authHeader.startsWith("Bearer ")) return null;
      const token = authHeader.split(" ")[1];
      try {
        const resp = await fetch(`https://oauth2.googleapis.com/tokeninfo?id_token=${token}`);
        if (!resp.ok) return null;
        const data = await resp.json();
        return data.sub; 
      } catch (e) {
        return null;
      }
    }

    try {
      // 1. Sync Multi-Tenant Database
      if (url.pathname.includes("/wifey_db.json")) {
        const key = url.pathname.substring(1); 
        
        if (request.method === "GET") {
          const object = await env.FIRMWARE_BUCKET.get(key);
          if (!object) return new Response(JSON.stringify({}), { status: 404, headers: corsHeaders });
          return new Response(object.body, { headers: { ...corsHeaders, "Content-Type": "application/json" } });
        }
        
        if (request.method === "PUT" || request.method === "POST") {
          const userId = await getUserId(request);
          if (!userId || !key.includes(userId)) {
            return new Response(JSON.stringify({ error: "Unauthorized" }), { status: 401, headers: corsHeaders });
          }
          await env.FIRMWARE_BUCKET.put(key, request.body, { httpMetadata: { contentType: "application/json" } });
          return new Response(JSON.stringify({ success: true }), { headers: corsHeaders });
        }
      }

      // 2. Upload Firmware Binary (Isolated per user)
      if (url.pathname === "/api/firmware/upload" && request.method === "POST") {
        const userId = await getUserId(request);
        if (!userId) return new Response(JSON.stringify({ error: "Unauthorized" }), { status: 401, headers: corsHeaders });
      
        const fleet = request.headers.get("X-Fleet-ID") || "DefaultFleet";
        const buildNum = request.headers.get("X-Build-Num") || "100";
        
        const formData = await request.formData();
        const file = formData.get("file");
        if (!file) return new Response(JSON.stringify({ error: "No file provided" }), { status: 400, headers: corsHeaders });
        
        const objectKey = `users/${userId}/${fleet}/firmware_b${buildNum}.bin`;
        const fileUrl = `${url.origin}/${objectKey}`;
      
        // Save the firmware binary
        await env.FIRMWARE_BUCKET.put(objectKey, await file.arrayBuffer(), {
          httpMetadata: { contentType: "application/octet-stream" }
        });
      
        // NEW: Auto-generate a static manifest file in the cloud for the ESP32 to check
        const manifestKey = `users/${userId}/${fleet}/manifest.json`;
        const manifestData = JSON.stringify({
          version: parseInt(buildNum),
          firmware_url: fileUrl,  // Included both common naming conventions
          download_url: fileUrl
        });
        
        await env.FIRMWARE_BUCKET.put(manifestKey, manifestData, {
          httpMetadata: { contentType: "application/json" }
        });
        
        return new Response(JSON.stringify({ success: true, download_url: fileUrl }), { headers: corsHeaders });
      }
      // 3. Download Firmware Binary (Public GET for ESP32)
      if (request.method === "GET") {
        const objectKey = url.pathname.substring(1);
        const object = await env.FIRMWARE_BUCKET.get(objectKey);
        if (object) {
          const headers = new Headers(corsHeaders);
          object.writeHttpMetadata(headers);
          
          // CRITICAL FOR ESP32 OTA: Pass the exact file size back to the hardware
          headers.set("Content-Length", object.size.toString());
          headers.set("etag", object.httpEtag);
          
          return new Response(object.body, { headers });
        }
      }

      return new Response("Not found", { status: 404, headers: corsHeaders });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), { status: 500, headers: corsHeaders });
    }
  }
};