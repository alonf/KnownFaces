const el = (id) => document.getElementById(id);
const forms = document.forms;
let current,
  cursor,
  nameFilter = "";
const text = (tag, value) =>
  Object.assign(document.createElement(tag), { textContent: value });
function message(value) {
  const dialog = document.querySelector("dialog[open]");
  if (!dialog) {
    el("status").textContent = value;
    return;
  }
  let notice = dialog.querySelector(".notice");
  if (!notice) {
    notice = text("p", "");
    notice.className = "notice";
    notice.setAttribute("role", "status");
    dialog.append(notice);
  }
  notice.textContent = value;
}
async function api(path, options = {}) {
  options.headers = { ...options.headers, "X-KnownFaces-UI": "1" };
  const response = await fetch(path, options);
  if (!response.ok) {
    if (response.status === 401) {
      document.querySelector("dialog[open]")?.close();
      signed(false);
    }
    const reason = await response.json().catch(() => ({}));
    throw Error(
      typeof reason.detail === "string" ? reason.detail : "Request failed",
    );
  }
  return response.status === 204 ? null : response.json();
}
const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
function signed(yes) {
  el("signin").hidden = yes;
  el("directory").hidden = !yes;
  el("logout").hidden = !yes;
}
function run(fn) {
  return async (event) => {
    event?.preventDefault();
    try {
      await fn(event);
    } catch (e) {
      message(e.message);
    }
  };
}
function portrait(sample) {
  const img = new Image();
  img.alt = "Face crop";
  if (sample) img.src = "/v1/samples/" + sample + "/crop";
  return img;
}
function date(time) {
  return time ? new Date(time).toLocaleString() : "No camera sightings";
}
async function list(more = false) {
  const query = new URLSearchParams({ cursor: more ? cursor : "" });
  if (nameFilter) query.set("name", nameFilter);
  const data = await api("/v1/people?" + query);
  signed(true);
  if (!more) el("cards").replaceChildren();
  for (const p of data.people) {
    const b = document.createElement("button");
    b.className = "card";
    b.append(
      portrait(p.sample_id),
      text("strong", p.full_name || "Unnamed visitor"),
      text("small", date(p.last_seen_at)),
    );
    b.onclick = run(() => details(p));
    el("cards").append(b);
  }
  cursor = data.next_cursor;
  el("more").hidden = !cursor;
  el("empty").hidden = !!el("cards").children.length;
}
async function details(p) {
  current = p.person_id;
  const [history, group] = await Promise.all([
    api("/v1/sightings?limit=10&person_id=" + current),
    api("/v1/people/" + current + "/co-occurrences"),
  ]);
  el("portrait").src = portrait(p.sample_id).src;
  el("identity").textContent = current;
  forms.tag.elements.name.value = p.full_name || "";
  el("history").replaceChildren();
  el("together").replaceChildren();
  for (const s of history.sightings) {
    const row = text("div", "");
    row.className = "entry";
    const a = text("a", s.camera_id + " · " + date(s.captured_at));
    a.href = "/v1/snapshots/" + s.snapshot_id + "/image";
    a.target = "_blank";
    a.rel = "noopener";
    const obs = s.faces.find((f) => f.person_id === current);
    row.append(a);
    if (obs) {
      const b = text("button", "Reassign");
      b.onclick = run(async () => {
        const target = prompt("Correct identity ID");
        if (target) {
          await api(
            "/v1/observations/" + obs.observation_id + "/person",
            json("PUT", { person_id: target }),
          );
          await details(p);
          await list();
        }
      });
      row.append(" ", b);
    }
    el("history").append(row);
  }
  if (!history.sightings.length)
    el("history").textContent = "No camera sightings.";
  for (const g of group.cooccurrences)
    el("together").append(
      text(
        "p",
        (g.full_name || g.person_id) + " · " + g.count + " shared images",
      ),
    );
  if (!group.cooccurrences.length)
    el("together").textContent = "No shared images.";
  if (!el("detail").open) el("detail").showModal();
}
forms.login.onsubmit = run(async () => {
  await api(
    "/login",
    json("POST", { token: forms.login.elements.token.value }),
  );
  forms.login.reset();
  message("");
  await list();
});
el("logout").onclick = run(async () => {
  await api("/v1/logout", { method: "POST" });
  signed(false);
  el("cards").replaceChildren();
});
forms.search.onsubmit = run(() => {
  nameFilter = forms.search.elements.name.value.trim();
  return list();
});
forms.search.onreset = () => {
  nameFilter = "";
  run(() => list())();
};
el("more").onclick = run(() => list(true));
async function finishName(options) {
  await api("/v1/people/" + current + "/name", options);
  el("detail").close();
  await list();
}
forms.tag.onsubmit = run(() =>
  finishName(json("PUT", { full_name: forms.tag.elements.name.value })),
);
el("clearName").onclick = run(() => finishName({ method: "DELETE" }));
for (const b of document.querySelectorAll(".close"))
  b.onclick = () => b.closest("dialog").close();
const enroll = forms.enroll;
el("upload").onclick = run(async () => {
  enroll.reset();
  el("choices").replaceChildren(text("legend", "Select a face"));
  el("saveRef").disabled = true;
  const select = enroll.elements.person_id;
  select.replaceChildren(new Option("New identity", ""));
  el("newName").hidden = false;
  let next = "";
  do {
    const data = await api("/v1/people?limit=200&cursor=" + next);
    for (const p of data.people)
      select.add(
        new Option(
          (p.full_name || "Unnamed") + " · " + p.person_id,
          p.person_id,
        ),
      );
    next = data.next_cursor;
  } while (next);
  el("reference").showModal();
});
enroll.elements.person_id.onchange = () => {
  el("newName").hidden = !!enroll.elements.person_id.value;
};
enroll.elements.image.onchange = run(async () => {
  el("saveRef").disabled = true;
  el("choices").replaceChildren(text("legend", "Select a face"));
  const file = enroll.elements.image.files[0];
  if (!file) return;
  const data = new FormData();
  data.set("image", file);
  const res = await api("/v1/references/preview", {
    method: "POST",
    body: data,
  });
  if (file !== enroll.elements.image.files[0]) return;
  for (const f of res.faces) {
    const l = document.createElement("label"),
      r = document.createElement("input"),
      img = new Image();
    r.type = "radio";
    r.name = "index";
    r.value = f.index;
    r.required = true;
    r.checked = res.faces.length === 1;
    img.src = "data:image/jpeg;base64," + f.crop_base64;
    img.alt = "Detected face " + (f.index + 1);
    l.append(r, img);
    el("choices").append(l);
  }
  el("saveRef").disabled = !res.faces.length;
  message(res.faces.length ? "" : "No face detected. Try a clearer photo.");
});
enroll.onsubmit = run(async () => {
  const data = new FormData(enroll);
  if (data.get("person_id")) data.delete("full_name");
  else data.delete("person_id");
  await api("/v1/references", { method: "POST", body: data });
  el("reference").close();
  await list();
  message("Reference saved.");
});
list().catch((e) => {
  if (e.message !== "Sign in") message(e.message);
});
