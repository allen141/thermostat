(() => {
  const realFetch = window.fetch.bind(window);
  const startedAt = Date.now();

  function json(value, status = 200) {
    return Promise.resolve(new Response(JSON.stringify(value), {
      status,
      headers: {"Content-Type": "application/json"},
    }));
  }

  function sampleAt(time) {
    const minutes = Math.floor((startedAt - time) / 60000);
    const cooling = minutes % 180 < 42;
    const wave = Math.sin(minutes / 45) * 1.4;
    return {
      captured_at: new Date(time).toISOString(),
      indoor_temp: 73.2 + wave - (cooling ? 0.7 : 0),
      outdoor_temp: 88.4,
      indoor_humidity: 47 + Math.sin(minutes / 90) * 3,
      cool_setpoint: 72,
      operation_mode: cooling ? "Cooling" : "EquipmentOff",
      fan_request: cooling,
      is_alive: true,
    };
  }

  function history(hours) {
    const interval = hours > 24 * 7 ? 30 * 60000 : 5 * 60000;
    const samples = [];
    for (let time = startedAt - hours * 3600000; time <= startedAt; time += interval) {
      samples.push(sampleAt(time));
    }
    return {
      samples,
      transitions: [
        {
          occurred_at: new Date(startedAt - 42 * 60000).toISOString(),
          field: "operation_mode",
          from_value: "EquipmentOff",
          to_value: "Cooling",
        },
        {
          occurred_at: new Date(startedAt - 3 * 3600000).toISOString(),
          field: "cool_setpoint",
          from_value: "74",
          to_value: "72",
        },
      ],
      observations: [
        {
          occurred_at: new Date(startedAt - 18 * 60000).toISOString(),
          kind: "outdoor_running",
          note: "Fixture observation for review",
        },
      ],
    };
  }

  window.fetch = (input, options = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const path = url.pathname.replace(/^\/thermostat/, "");
    const method = (options.method || "GET").toUpperCase();

    if (path === "/api/status") {
      return json({
        connected: true,
        device: {
          device_id: "preview-thermostat",
          device_name: "Living Room",
          location_name: "Preview Home",
        },
        latest: sampleAt(startedAt),
        configuration: {systemConfiguration: {coolingStages: 2}},
        last_poll: {at: new Date(startedAt).toISOString(), ok: true},
        poll_seconds: 300,
      });
    }
    if (path === "/api/homekit/status") {
      return json({ready: false, paired: false, current: [], events: []});
    }
    if (path === "/api/history") {
      return json(history(Math.min(8760, Number(url.searchParams.get("hours")) || 24)));
    }
    if (path === "/api/raw/latest") {
      return json({
        preview: true,
        device: "Living Room",
        operationStatus: {mode: "Cooling", fanRequest: true},
        indoorTemperature: 73.2,
      });
    }
    if (path === "/api/poll" && method === "POST") {
      return json({preview: true, refreshed_at: new Date().toISOString()});
    }
    if (path === "/api/observations" && method === "POST") {
      return json({id: "preview", occurred_at: new Date().toISOString()}, 201);
    }
    return realFetch(input, options);
  };

  document.addEventListener("click", event => {
    const exportLink = event.target.closest('a[href="/api/export.csv"]');
    if (!exportLink) return;
    event.preventDefault();
    alert("CSV export is disabled in the fixture-backed review preview.");
  });

  const banner = document.createElement("div");
  banner.className = "preview-banner";
  banner.textContent = "Review preview · fixture data · production is unaffected";
  document.body.prepend(banner);
})();
