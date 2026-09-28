"use strict";

(() => {
  // Keep the array valid JSON; the provenance test reads it directly.
  const ASSETS = Object.freeze([
    {
      "kind": "style",
      "url": "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.0/swagger-ui.css",
      "integrity": "sha384-Ov4/wv3j2bmct8cDc5X4ngJZohVPzEmc6uDPH8WeljUxO5vtoykvMEfbu9Vh6RaW"
    },
    {
      "kind": "script",
      "url": "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.0/swagger-ui-bundle.js",
      "integrity": "sha384-YDALVcy8kj8yltLBVi1vBiBAUqdxvus673gM8XKwiy6aDUJFXivF/KCufekjYbVf"
    }
  ]);
  const button = document.getElementById("load-swagger");
  const status = document.getElementById("viewer-status");
  const raw = document.getElementById("spec-json");
  const section = document.getElementById("swagger-section");

  function showError(message) {
    status.textContent = message;
    status.dataset.error = "true";
  }

  async function localSpecification() {
    const allowedHosts = new Set(["127.0.0.1", "localhost", "[::1]"]);
    if (!allowedHosts.has(location.hostname) || !["http:", "https:"].includes(location.protocol)) {
      throw new Error("Serve docs/api on loopback using the README command. The direct JSON file remains readable offline.");
    }
    const response = await fetch(new URL("openapi.json", location.href), {
      credentials: "omit",
      mode: "same-origin",
      redirect: "error",
      cache: "no-store",
      referrerPolicy: "no-referrer",
    });
    if (!response.ok) {
      throw new Error("The local specification could not be loaded. Check that docs/api is the served directory.");
    }
    return response.json();
  }

  function requireLocalReferences(value) {
    if (value === null || typeof value !== "object") return;
    for (const [key, child] of Object.entries(value)) {
      if (["$ref", "$dynamicRef"].includes(key) && (typeof child !== "string" || !child.startsWith("#/"))) {
        throw new Error("This viewer accepts only references inside the local specification.");
      }
      if (key === "externalValue") {
        throw new Error("Remote example resources are not supported in this local viewer.");
      }
      requireLocalReferences(child);
    }
  }

  function loadAsset(asset) {
    return new Promise((resolve, reject) => {
      // Only the literal allowlist above supplies URLs, never query strings or fetched configuration.
      const element = document.createElement(asset.kind === "style" ? "link" : "script");
      if (asset.kind === "style") {
        element.rel = "stylesheet";
        element.href = asset.url;
      } else {
        element.src = asset.url;
        element.async = true;
      }
      element.integrity = asset.integrity;
      element.crossOrigin = "anonymous";
      element.referrerPolicy = "no-referrer";
      const timer = setTimeout(() => {
        element.remove();
        reject(new Error("The pinned Swagger assets did not load in time. The local JSON is still available; reload to retry."));
      }, 30000);
      element.onload = () => { clearTimeout(timer); resolve(); };
      element.onerror = () => {
        clearTimeout(timer);
        element.remove();
        reject(new Error("A pinned Swagger asset was unavailable or failed its integrity check. The local JSON is still available."));
      };
      document.head.append(element);
    });
  }

  function readOnlyAuthPlugin() {
    // Removing these components means no credential-entry form is mounted.
    return {
      components: {
        authorizeBtn: () => null,
        authorizeOperationBtn: () => null,
        authorizationPopup: () => null,
      },
    };
  }

  function renderSwagger(spec) {
    section.hidden = false;
    // A parsed local spec cannot be replaced by a URL supplied through Swagger's toolbar or query config.
    SwaggerUIBundle({
      spec,
      dom_id: "#swagger-ui",
      layout: "BaseLayout",
      presets: [SwaggerUIBundle.presets.apis],
      plugins: [readOnlyAuthPlugin],
      supportedSubmitMethods: [],
      tryItOutEnabled: false,
      validatorUrl: null,
      persistAuthorization: false,
      withCredentials: false,
      queryConfigEnabled: false,
      useUnsafeMarkdown: false,
      deepLinking: false,
      docExpansion: "full",
      defaultModelRendering: "example",
      defaultModelsExpandDepth: 1,
      requestInterceptor: () => {
        throw new Error("This documentation viewer cannot send requests. Use the local contract tests instead.");
      },
      onComplete: () => {
        status.textContent = "Read-only Swagger UI loaded. No fixture API requests can be submitted here.";
      },
    });
  }

  async function initialize() {
    const spec = await localSpecification();
    raw.textContent = JSON.stringify(spec, null, 2);
    document.getElementById("fixture-auth").textContent = spec.components.securitySchemes.fixtureBearer.description;
    requireLocalReferences(spec);
    button.disabled = false;
    status.textContent = "Local JSON ready. No third-party assets have been requested.";
    button.addEventListener("click", async () => {
      button.disabled = true;
      status.textContent = "Loading the two pinned Swagger UI assets. No API requests are sent.";
      try {
        await Promise.all(ASSETS.map(loadAsset));
        if (typeof SwaggerUIBundle !== "function") {
          throw new Error("Swagger UI did not initialize. The local JSON is still available.");
        }
        renderSwagger(spec);
        button.hidden = true;
      } catch (error) {
        showError(error.message);
      }
    }, { once: true });
  }

  initialize().catch((error) => showError(error.message));
})();
