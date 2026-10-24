"use strict";
const $ = id => document.getElementById(id);
let scene = null;
let socket;
let reconnectTimer;
let manual = null;
const strokeNodes = new Map();
const svgNS = "http://www.w3.org/2000/svg";
function message(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
}
async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}
function acceptScene(next) {
  if (scene && next.revision < scene.revision) return;
  if (scene && next.generation !== scene.generation) abandonManual();
  scene = next;
  const activeIds = new Set();
  for (const object of scene.objects) for (const stroke of object.strokes) {
    activeIds.add(stroke.stroke_id);
    let node = strokeNodes.get(stroke.stroke_id);
    if (!node) {
      node = document.createElementNS(svgNS, "polyline");
      for (const [key, value] of Object.entries({fill:"none", stroke:object.source === "user" ? "#337e81" : "#283e39", "stroke-width":"3", "stroke-linecap":"round", "stroke-linejoin":"round"})) node.setAttribute(key, value);
      node.dataset.strokeId = stroke.stroke_id;
      strokeNodes.set(stroke.stroke_id, node);
      $("ink").appendChild(node);
    }
    // Only server-confirmed executed geometry becomes permanent ink.
    node.setAttribute("points", stroke.executed_points.map(p => `${p.x*1000},${p.y*1000}`).join(" "));
  }
  for (const [id, node] of strokeNodes) if (!activeIds.has(id)) { node.remove(); strokeNodes.delete(id); }
  $("status").textContent = scene.status[0].toUpperCase() + scene.status.slice(1);
  const strokes = scene.objects.flatMap(o => o.strokes);
  const current = strokes.find(s => s.stroke_id === scene.current_stroke_id);
  $("progress").textContent = current ? `${Math.round(current.progress*100)}% of current stroke` : `${scene.completed_stroke_ids.length} completed`;
  $("revision").textContent = `Revision ${scene.revision}`;
  $("scene-summary").textContent = `${scene.objects.length} objects · ${scene.pending_stroke_ids.length} pending · ${scene.completed_stroke_ids.length} completed · ${scene.cancelled_stroke_ids.length} cancelled`;
  if (document.activeElement !== $("speed")) $("speed").value = scene.speed;
  $("speed-value").textContent = `${Number(scene.speed).toFixed(2)} units/s`;
  if ($("scene-json").parentElement.open) renderInspector();
}
function renderInspector() { $("scene-json").textContent = JSON.stringify(scene, null, 2); }
$("scene-json").parentElement.addEventListener("toggle", renderInspector);
async function refresh() { acceptScene(await request("/scene")); }
function connect() {
  socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket.onopen = () => { $("connection").textContent = "● Live connection"; refresh().catch(e => message(e.message, true)); };
  socket.onmessage = event => { const data = JSON.parse(event.data); if (data.type === "scene") acceptScene(data.scene); };
  socket.onclose = () => { $("connection").textContent = "Reconnecting…"; reconnectTimer = setTimeout(connect, 1000); };
  socket.onerror = () => socket.close();
}
async function loadExample() {
  try {
    const command = await request(`/examples/${$("example").value}.json`);
    const suffix = crypto.randomUUID().slice(0, 8);
    command.object_id += `-${suffix}`;
    command.paths.forEach(p => { p.stroke_id += `-${suffix}`; });
    $("command").value = JSON.stringify(command, null, 2);
    message("Ready. Example IDs are unique each time you load one.");
  } catch (e) { message(e.message, true); }
}
$("example").addEventListener("change", loadExample);
$("execute").addEventListener("click", async () => {
  $("execute").disabled = true;
  try {
    const command = JSON.parse($("command").value);
    if (scene && command.expected_generation === undefined) command.expected_generation = scene.generation;
    acceptScene(await request("/commands/draw", command));
    message(`Queued ${command.object_id}. Change IDs or load an example to add another object.`);
  } catch (e) { message(e.message, true); }
  finally { $("execute").disabled = false; }
});
for (const button of document.querySelectorAll("[data-action]")) button.addEventListener("click", async () => {
  try { acceptScene(await request(`/commands/${button.dataset.action}`, {})); message(`${button.textContent} applied.`); }
  catch (e) { message(e.message, true); }
});
$("refresh").addEventListener("click", () => refresh().catch(e => message(e.message, true)));
$("speed").addEventListener("input", () => { $("speed-value").textContent = `${Number($("speed").value).toFixed(2)} units/s`; });
$("speed").addEventListener("change", async () => {
  try { acceptScene(await request("/commands/speed", {speed: Number($("speed").value)})); }
  catch (e) { message(e.message, true); }
});
$("pen").addEventListener("change", () => { $("board").classList.toggle("pen", $("pen").checked); abandonManual(); });
function position(event) {
  const rect = $("board").getBoundingClientRect();
  return {x: Math.max(0, Math.min(1, (event.clientX-rect.left)/rect.width)), y: Math.max(0, Math.min(1, (event.clientY-rect.top)/rect.height))};
}
function previewManual() { $("manual-preview").setAttribute("points", manual ? manual.points.map(p => `${p.x*1000},${p.y*1000}`).join(" ") : ""); }
function abandonManual() { manual = null; previewManual(); }
$("board").addEventListener("pointerdown", event => {
  if (!$("pen").checked || !scene || manual || event.button !== 0) return;
  event.preventDefault();
  manual = {points:[position(event)], generation:scene.generation, pointerId:event.pointerId};
  $("board").setPointerCapture(event.pointerId);
  previewManual();
});
$("board").addEventListener("pointermove", event => {
  if (!manual || manual.pointerId !== event.pointerId) return;
  const point = position(event), last = manual.points.at(-1);
  if (Math.hypot(point.x-last.x, point.y-last.y) > .001 && manual.points.length < 4095) manual.points.push(point);
  previewManual();
});
$("board").addEventListener("pointerup", async event => {
  if (!manual || manual.pointerId !== event.pointerId) return;
  const finished = manual;
  finished.points.push(position(event));
  abandonManual();
  const id = `user-${crypto.randomUUID()}`;
  try {
    const session = window.astraSessionId;
    const reactive = $("react-to-ink").checked && session;
    const endpoint = reactive ? `/commands/manual?session_id=${encodeURIComponent(session)}` : "/commands/manual";
    acceptScene(await request(endpoint, {object_id:id, expected_generation:finished.generation, paths:[{stroke_id:`${id}-stroke`, geometry:{type:"polyline", points:finished.points}}]}));
    message("User stroke saved to the scene.");
  } catch (e) { message(`User stroke was not saved: ${e.message}`, true); }
});
$("board").addEventListener("pointercancel", abandonManual);
window.addEventListener("pagehide", () => { clearTimeout(reconnectTimer); if (socket) { socket.onclose = null; socket.close(); } });
loadExample();
connect();
