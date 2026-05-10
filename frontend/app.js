const $ = s => document.querySelector(s)
const $$ = s => document.querySelectorAll(s)
const EXIF_NAMES = { 1: "novelai", 2: "sd", 3: "comfy", 4: "mj", 5: "celsys", 6: "photoshop", 7: "stealth" }
const LONG_DIGITS_RE = /\d{6,}/g


$$(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach(t => t.classList.remove("active"))
    $$(".panel").forEach(p => p.classList.remove("active"))
    tab.classList.add("active")
    $(`#${tab.dataset.tab}`).classList.add("active")
    if (tab.dataset.tab === "explorer") loadSearches()
    if (tab.dataset.tab === "progress") loadProgress()
  })
})

$("#btn-submit").addEventListener("click", async () => {
  const url = $("#input-url").value.trim()
  if (!url) return
  const pages = parseInt($("#input-pages").value) || 30
  const mode = $("#input-mode").value
  const action = $("#input-action").value
  const status = $("#submit-status")
  status.textContent = "Submitting..."
  status.className = ""

  const userIds = [...new Set(url.match(LONG_DIGITS_RE) || [])]
  const hasSearch = /search/i.test(url)
  const isUserInput = userIds.length > 0 && !hasSearch

  if (userIds.length > 0 && hasSearch) {
    status.textContent = "Ambiguous input - pick either user IDs or a search URL"
    status.className = "status-err"
    return
  }

  try {
    let resp
    if (isUserInput) {
      resp = await fetch("/api/submit_users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_ids: userIds, action })
      })
    } else {
      resp = await fetch("/api/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, pages, mode, action })
      })
    }
    const data = await resp.json()
    status.textContent = `Submitted as ${data.id} - you can close this page`
    status.className = "status-ok"
  } catch (e) {
    status.textContent = `Error: ${e.message}`
    status.className = "status-err"
  }
})

async function loadSearches() {
  const list = $("#search-list")
  const detail = $("#search-detail")
  detail.classList.add("hidden")
  list.innerHTML = "Loading..."
  try {
    const resp = await fetch("/api/searches")
    const data = await resp.json()
    if (!data.length) { list.innerHTML = "No searches yet"; return }
    list.innerHTML = data.map(s => {
      const d = new Date(parseInt(s.created_at) * 1000)
      const ts = d.toLocaleString()
      return `<div class="search-item" data-id="${s.id}">
        <span class="id">${s.id}</span>
        <span class="time">${ts}</span>
        <span class="search-actions">
          <button class="btn-icon btn-rename" title="Rename">&#9998;</button>
          <button class="btn-icon btn-delete" title="Delete">&times;</button>
        </span>
      </div>`
    }).join("")
    list.querySelectorAll(".search-item").forEach(el => {
      el.querySelector(".id").addEventListener("click", () => openSearch(el.dataset.id))
      el.querySelector(".btn-rename").addEventListener("click", e => { e.stopPropagation(); renameSearch(el.dataset.id) })
      el.querySelector(".btn-delete").addEventListener("click", e => { e.stopPropagation(); deleteSearch(el.dataset.id) })
    })
  } catch (e) {
    list.innerHTML = `Error: ${e.message}`
  }
}

async function openSearch(id) {
  const list = $("#search-list")
  const detail = $("#search-detail")
  list.innerHTML = ""
  detail.classList.remove("hidden")
  $("#detail-title").textContent = id
  $("#detail-stats").textContent = "Loading..."
  $("#results-grid").innerHTML = ""

  try {
    const resp = await fetch(`/api/search/${id}`)
    const data = await resp.json()
    if (data.error) { $("#detail-stats").textContent = data.error; return }

    const total = data.post_ids.length
    const scannedCount = Object.keys(data.scanned).length
    const allScanned = scannedCount >= total
    const typeCounts = {}
    for (const s of Object.values(data.scanned)) {
      if (s.exif_type) {
        const name = EXIF_NAMES[s.exif_type] || "?"
        typeCounts[name] = (typeCounts[name] || 0) + 1
      }
    }
    const typeParts = Object.entries(typeCounts)
      .sort((a, b) => b[1] - a[1])
      .map(([name, n]) => `${n} ${name}`)
    const statParts = [`${scannedCount}/${total} scanned`, ...typeParts]
    $("#detail-stats").textContent = statParts.join(" | ")

    const scanBtn = $("#btn-scan")
    if (allScanned) {
      scanBtn.textContent = "Scanned"
      scanBtn.disabled = true
    } else {
      scanBtn.textContent = `Scan (${total - scannedCount} remaining)`
      scanBtn.disabled = false
      scanBtn.onclick = async () => {
        scanBtn.disabled = true
        scanBtn.textContent = "Scanning..."
        const r = await fetch("/api/scan", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ search_id: id })
        })
        const d = await r.json()
        scanBtn.textContent = d.status === "already_scanned" ? "Scanned" : `Scanning ${d.to_scan}...`
      }
    }

    $("#btn-show").onclick = () => renderResults(data)
    $("#btn-back").onclick = () => { detail.classList.add("hidden"); loadSearches() }
  } catch (e) {
    $("#detail-stats").textContent = e.message
  }
}

function pageSuffix(url) {
  if (!url) return ""
  const m = url.match(/_p(\d+)\./)
  return m ? `_p${m[1]}` : ""
}

function renderResults(data) {
  const grid = $("#results-grid")
  grid.innerHTML = data.post_ids.map(pid => {
    const s = data.scanned[pid]
    const pg = s ? pageSuffix(s.url) : ""
    const label = pid + pg
    let badge = ""
    if (!s) {
      badge = `<span class="not-scanned">not scanned</span>`
    } else if (s.exif_type) {
      const name = EXIF_NAMES[s.exif_type] || "?"
      badge = `<span class="exif-badge exif-${s.exif_type}">${name}</span>`
    } else {
      badge = `<span class="no-exif">NIL</span>`
    }
    return `<div class="result-card" data-pid="${pid}">
      <span class="result-link">${label}</span><br>${badge}
    </div>`
  }).join("")
  grid.querySelectorAll(".result-link").forEach(el => {
    el.addEventListener("click", () => {
      const pid = el.closest(".result-card").dataset.pid
      window.open(`https://www.pixiv.net/artworks/${pid}`, "_blank")
    })
  })
}

function esc(s) {
  const d = document.createElement("div")
  d.textContent = s
  return d.innerHTML
}

async function deleteSearch(id) {
  if (!confirm(`Delete search "${id}"?`)) return
  await fetch(`/api/search/${id}`, { method: "DELETE" })
  loadSearches()
}

async function renameSearch(id) {
  const newId = prompt("New name:", id)
  if (!newId || newId === id) return
  await fetch(`/api/search/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_id: newId })
  })
  loadSearches()
}

async function loadProgress() {
  const el = $("#progress-list")
  try {
    const resp = await fetch("/api/progress")
    const tasks = await resp.json()
    if (!tasks.length) { el.innerHTML = '<div class="progress-empty">No active tasks</div>'; return }
    el.innerHTML = tasks.map(t => {
      const pct = t.total > 0 ? Math.round(t.done / t.total * 100) : 0
      const label = t.total > 0 ? `${t.done} / ${t.total}` : "..."
      return `<div class="progress-item">
        <div class="progress-info">
          <span class="progress-id">${t.id}</span>
          <span class="progress-type">${t.type}</span>
          <span class="progress-phase">${t.phase}</span>
          <span class="progress-label">${label}</span>
        </div>
        <div class="progress-bar-bg"><div class="progress-bar-fill" style="width:${pct}%"></div></div>
      </div>`
    }).join("")
  } catch (e) {
    el.innerHTML = `<div class="progress-empty">Error: ${e.message}</div>`
  }
}
