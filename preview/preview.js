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

  function unitHistory(id, hours) {
    const data = history(hours);
    const offset = id === "sensi" ? 3.1 : 0;
    data.unit_id = id;
    data.source_id = `${id}-homekit`;
    data.samples = data.samples.map(sample => ({...sample, unit_id: id, source_id: `${id}-homekit`, indoor_temp: sample.indoor_temp + offset, cool_setpoint: sample.cool_setpoint + offset}));
    data.observations = data.observations.map(item => ({...item, unit_id: id}));
    return data;
  }

  function previewUnit(id) {
    const sensi = id === "sensi";
    const sample = sampleAt(startedAt);
    return {id, display_name: sensi ? "Sensi" : "T10", vendor: sensi ? "Copeland" : "Resideo", model: sensi ? "1F95U-42WF" : "T10", active_source: `${id}-homekit`, connected: true, resideo_connected: !sensi, latest: {...sample, unit_id: id, source_id: `${id}-homekit`, indoor_temp: sample.indoor_temp + (sensi ? 3.1 : 0), cool_setpoint: sample.cool_setpoint + (sensi ? 3.1 : 0)}, sources: [{id: `${id}-homekit`, unit_id: id, kind: "homekit", enabled: 1}], homekit: {ready: true, paired: true, collection_mode: "events_only", poll_seconds: 0, accessories: [{unit_id: id, name: sensi ? "Upstairs Sensi" : "Living Room T10", model: sensi ? "1F95U-42WF" : "T10", connection_state: "connected", last_connected_at: new Date(startedAt).toISOString()}]}};
  }

  window.fetch = (input, options = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const path = url.pathname.replace(/^\/thermostat/, "");
    const method = (options.method || "GET").toUpperCase();

    if (path === "/api/units") return json({units: [previewUnit("t10"), previewUnit("sensi")]});
    const unitHistoryMatch = path.match(/^\/api\/units\/(t10|sensi)\/history$/);
    if (unitHistoryMatch) return json(unitHistory(unitHistoryMatch[1], Math.min(8760, Number(url.searchParams.get("hours")) || 24)));
    const unitPollMatch = path.match(/^\/api\/units\/(t10|sensi)\/poll$/);
    if (unitPollMatch && method === "POST") return json({preview: true, unit_id: unitPollMatch[1]});
    if (path === "/api/compare") return json({hours: Number(url.searchParams.get("hours")) || 24, units: [unitHistory("t10", Number(url.searchParams.get("hours")) || 24), unitHistory("sensi", Number(url.searchParams.get("hours")) || 24)]});
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
    const exportLink = event.target.closest('a[href^="/api/export.csv"]');
    if (!exportLink) return;
    event.preventDefault();
    alert("CSV export is disabled in the fixture-backed review preview.");
  });

  const banner = document.createElement("div");
  banner.className = "preview-banner";
  banner.textContent = "Review preview · fixture data · production is unaffected";
  document.body.prepend(banner);
})();
