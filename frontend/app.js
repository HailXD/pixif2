const $ = s => document.querySelector(s)
const $$ = s => document.querySelectorAll(s)
const EXIF_NAMES = { 1: "novelai", 2: "sd", 3: "comfy", 4: "mj", 5: "celsys", 6: "photoshop", 7: "stealth" }
const EXIF_CODES = Object.keys(EXIF_NAMES).map(Number)
const NO_EXIF_CODE = 0
const FILTER_CODES = [...EXIF_CODES, NO_EXIF_CODE]
const EXIF_FILTER_KEY = "pixif2-exif-types"
const LONG_DIGITS_RE = /\d{6,}/g
const PAGE_SIZE = 60
const SEARCH_PAGE_SIZE = 5
let viewerScale = 1
let viewerDrag = null
let explorerEvents = null
let explorerPage = 1


$$(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    location.hash = `#/${tab.dataset.tab}`
  })
})

$("#btn-submit").addEventListener("click", async () => {
  const url = $("#input-url").value.trim()
  if (!url) return
  const pages = parseInt($("#input-pages").value) || 30
  const mode = $("#input-mode").value
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
        body: JSON.stringify({ user_ids: userIds })
      })
    } else {
      resp = await fetch("/api/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, pages, mode })
      })
    }
    const data = await resp.json()
    const ids = data.ids || [data.id]
    status.textContent = `Submitted as ${ids.join(", ")} - you can close this page`
    status.className = "status-ok"
  } catch (e) {
    status.textContent = `Error: ${e.message}`
    status.className = "status-err"
  }
})

function route() {
  const raw = location.hash.slice(2) || "submit"
  const [path, qs = ""] = raw.split("?")
  const parts = path.split("/").filter(Boolean)
  const tab = parts[0] || "submit"
  const params = new URLSearchParams(qs)
  if (tab === "progress") {
    location.hash = "#/explorer"
    return
  }
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab))
  $$(".panel").forEach(p => p.classList.toggle("active", p.id === tab))
  if (tab === "explorer") {
    if (parts[1]) {
      closeExplorerEvents()
      openSearch(decodeURIComponent(parts[1]), parseInt(params.get("page")) || 1)
    } else {
      loadSearches(parseInt(params.get("page")) || 1)
      openExplorerEvents()
    }
  } else {
    closeExplorerEvents()
  }
}


function explorerHash(id, page) {
  return `#/explorer/${encodeURIComponent(id)}?page=${page}`
}


async function loadSearches(page = 1) {
  explorerPage = page
  const list = $("#search-list")
  const detail = $("#search-detail")
  detail.classList.add("hidden")
  list.innerHTML = "Loading..."
  try {
    const [searchResp, taskResp] = await Promise.all([fetch(`/api/searches?page=${page}`), fetch("/api/progress")])
    const data = await searchResp.json()
    const tasks = await taskResp.json()
    const active = tasks.filter(t => t.type === "search" || t.type === "search+scan" || t.type === "user_search")
    const searches = data.items || []
    if (!searches.length && !active.length) { list.innerHTML = "No searches yet"; return }
    const activeHtml = active.map(t => {
      const label = t.total > 0 ? `${t.done}/${t.total}` : "..."
      return `<div class="search-item active-task" data-id="${esc(t.id)}">
        <span class="id">${esc(t.id)}</span>
        <span class="time">${esc(t.type)} ${esc(t.phase)} ${label}</span>
        <span class="search-status active-mark">...</span>
      </div>`
    }).join("")
    const savedHtml = searches.map(s => {
      const d = new Date(parseInt(s.created_at) * 1000)
      const ts = d.toLocaleString()
      return `<div class="search-item" data-id="${esc(s.id)}">
        <span class="id">${esc(s.id)}</span>
        <span class="time">${ts}</span>
        <span class="search-actions">
          <span class="search-status done-mark">✓</span>
          <button class="btn-icon btn-rename" title="Rename">&#9998;</button>
          <button class="btn-icon btn-delete" title="Delete">&times;</button>
        </span>
      </div>`
    }).join("")
    list.innerHTML = activeHtml + savedHtml + renderSearchPager(data)
    list.querySelectorAll(".search-item").forEach(el => {
      if (!el.dataset.id) return
      el.querySelector(".id").addEventListener("click", () => { location.hash = explorerHash(el.dataset.id, 1) })
      const rename = el.querySelector(".btn-rename")
      const del = el.querySelector(".btn-delete")
      if (rename) rename.addEventListener("click", e => { e.stopPropagation(); renameSearch(el.dataset.id) })
      if (del) del.addEventListener("click", e => { e.stopPropagation(); deleteSearch(el.dataset.id) })
    })
    const prev = $("#search-prev")
    const next = $("#search-next")
    if (prev) prev.onclick = () => { location.hash = `#/explorer?page=${Math.max(page - 1, 1)}` }
    if (next) next.onclick = () => { location.hash = `#/explorer?page=${Math.min(page + 1, data.pages)}` }
  } catch (e) {
    list.innerHTML = `Error: ${e.message}`
  }
}

function openExplorerEvents() {
  if (explorerEvents) return
  explorerEvents = new EventSource("/api/events")
  explorerEvents.onmessage = () => {
    const raw = location.hash.slice(2) || "submit"
    const [path] = raw.split("?")
    const parts = path.split("/").filter(Boolean)
    if ((parts[0] || "submit") === "explorer" && !parts[1]) loadSearches(explorerPage)
  }
}

function closeExplorerEvents() {
  if (!explorerEvents) return
  explorerEvents.close()
  explorerEvents = null
}

function renderSearchPager(data) {
  if (!data.total || data.pages <= 1) return ""
  const start = (data.page - 1) * SEARCH_PAGE_SIZE + 1
  const end = Math.min(data.page * SEARCH_PAGE_SIZE, data.total)
  return `<div class="list-pager">
    <button class="btn-secondary" id="search-prev" ${data.page <= 1 ? "disabled" : ""}>Prev</button>
    <span>${start}-${end} of ${data.total} | page ${data.page}/${data.pages}</span>
    <button class="btn-secondary" id="search-next" ${data.page >= data.pages ? "disabled" : ""}>Next</button>
  </div>`
}

function getExifTypes() {
  const raw = localStorage.getItem(EXIF_FILTER_KEY)
  if (raw === null) return [...EXIF_CODES]
  const types = raw.split(",").map(Number).filter(n => FILTER_CODES.includes(n))
  return types
}

function setExifTypes(types) {
  const sorted = [...new Set(types)].filter(n => FILTER_CODES.includes(n)).sort((a, b) => a - b)
  if (sorted.length === EXIF_CODES.length && sorted.every(n => EXIF_CODES.includes(n))) {
    localStorage.removeItem(EXIF_FILTER_KEY)
  } else {
    localStorage.setItem(EXIF_FILTER_KEY, sorted.join(","))
  }
  return sorted
}

function renderExifFilters(id) {
  const active = new Set(getExifTypes())
  const buttons = FILTER_CODES.map(code => {
    const name = code === NO_EXIF_CODE ? "None" : EXIF_NAMES[code]
    return `<button class="filter-btn${active.has(code) ? " active" : ""}" data-code="${code}">${esc(name)}</button>`
  }).join("")
  $("#exif-filters").innerHTML = `${buttons}
    <button class="filter-action" data-action="all">Select all</button>
    <button class="filter-action" data-action="none">Select none</button>
    <button class="filter-action update" data-action="update">Update</button>`
  $$("#exif-filters .filter-btn").forEach(btn => {
    btn.onclick = () => btn.classList.toggle("active")
  })
  $$("#exif-filters .filter-action").forEach(btn => {
    btn.onclick = () => {
      const action = btn.dataset.action
      const filters = [...$$("#exif-filters .filter-btn")]
      if (action === "all") filters.forEach(el => el.classList.add("active"))
      if (action === "none") filters.forEach(el => el.classList.remove("active"))
      if (action !== "update") return
      const types = filters.filter(el => el.classList.contains("active")).map(el => Number(el.dataset.code))
      setExifTypes(types)
      const hash = explorerHash(id, 1)
      if (location.hash === hash) openSearch(id, 1)
      else location.hash = hash
    }
  })
}

async function openSearch(id, page = 1) {
  const list = $("#search-list")
  const detail = $("#search-detail")
  const exifTypes = getExifTypes()
  list.innerHTML = ""
  detail.classList.remove("hidden")
  $("#detail-title").textContent = id
  $("#detail-stats").textContent = "Loading..."
  $("#pager").innerHTML = ""
  $("#results-grid").innerHTML = ""
  renderExifFilters(id)

  try {
    const typesParam = exifTypes.length ? exifTypes.join(",") : "none"
    const resp = await fetch(`/api/results/${encodeURIComponent(id)}?page=${page}&exif_types=${typesParam}`)
    const data = await resp.json()
    if (data.error) { $("#detail-stats").textContent = data.error; return }

    const allScanned = data.scanned_count >= data.raw_total
    $("#detail-stats").textContent = `${data.total}/${data.raw_total} shown | ${data.scanned_count}/${data.raw_total} scanned`
    if (!allScanned) resumeScan(id)

    $("#btn-back").onclick = () => { location.hash = "#/explorer" }
    renderPager(id, data)
    renderResults(data)
  } catch (e) {
    $("#detail-stats").textContent = e.message
  }
}

async function resumeScan(id) {
  await fetch("/api/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ search_id: id })
  })
}

function pageSuffix(url) {
  if (!url) return ""
  const m = url.match(/_p(\d+)\./)
  return m ? `_p${m[1]}` : ""
}

function renderResults(data) {
  const grid = $("#results-grid")
  const offset = (data.page - 1) * PAGE_SIZE
  grid.innerHTML = data.items.map((item, i) => {
    const pid = item.post_id
    const pg = item.url ? pageSuffix(item.url) : ""
    const label = pid + pg
    const index = offset + i + 1
    let badge = ""
    if (!item.scanned) {
      badge = `<span class="not-scanned">not scanned</span>`
    } else if (item.exif_type) {
      const name = EXIF_NAMES[item.exif_type] || "?"
      badge = `<span class="exif-badge exif-${item.exif_type}">${name}</span>`
    } else {
      badge = `<span class="no-exif">NIL</span>`
    }
    const download = item.download_url ? `<a class="download-link" href="${esc(item.download_url)}">Download</a>` : ""
    const thumb = item.image_url
      ? `<button class="thumb" data-preview="${esc(item.preview_url || item.full_image_url)}" data-download="${esc(item.download_url || "")}"><img src="${esc(item.image_url)}" loading="lazy" alt="${esc(label)}"></button>`
      : ""
    return `<div class="result-card" data-pid="${pid}">
      ${thumb}
      <div class="result-meta">
        <span class="result-link">${label}</span>
        ${download}
      </div>
      ${badge}
      <span class="result-index">${index}</span>
    </div>`
  }).join("")
  grid.querySelectorAll(".result-link").forEach(el => {
    el.addEventListener("click", () => {
      const pid = el.closest(".result-card").dataset.pid
      window.open(`https://www.pixiv.net/artworks/${pid}`, "_blank")
    })
  })
  grid.querySelectorAll(".thumb").forEach(el => {
    el.addEventListener("click", () => openViewer(el.dataset.preview, el.dataset.download))
    el.querySelector("img").addEventListener("load", () => el.classList.add("thumb-loaded"))
    el.querySelector("img").addEventListener("error", () => el.classList.add("thumb-error"))
  })
}

function openViewer(src, download) {
  if (!src) return
  viewerScale = 1
  $("#viewer-img").src = src
  $("#viewer-download").href = download || src
  $("#viewer").classList.remove("hidden")
  $("#viewer").setAttribute("aria-hidden", "false")
  applyViewerZoom()
}

function closeViewer() {
  $("#viewer").classList.add("hidden")
  $("#viewer").setAttribute("aria-hidden", "true")
  $("#viewer-img").src = ""
  $("#viewer-download").href = ""
  viewerDrag = null
  $("#viewer-stage").classList.remove("dragging")
}

function applyViewerZoom() {
  $("#viewer-img").style.transform = `scale(${viewerScale})`
  $("#viewer-zoom").textContent = `${Math.round(viewerScale * 100)}%`
}

$("#viewer-zoom-in").addEventListener("click", () => {
  viewerScale = Math.min(viewerScale + .25, 6)
  applyViewerZoom()
})

$("#viewer-zoom-out").addEventListener("click", () => {
  viewerScale = Math.max(viewerScale - .25, .25)
  applyViewerZoom()
})

$("#viewer-close").addEventListener("click", closeViewer)
$("#viewer").addEventListener("click", e => { if (e.target.id === "viewer") closeViewer() })
$("#viewer-stage").addEventListener("wheel", e => {
  e.preventDefault()
  viewerScale = Math.min(Math.max(viewerScale + (e.deltaY < 0 ? .15 : -.15), .25), 6)
  applyViewerZoom()
})
$("#viewer-stage").addEventListener("pointerdown", e => {
  if (e.button !== 0) return
  const stage = $("#viewer-stage")
  viewerDrag = {
    x: e.clientX,
    y: e.clientY,
    left: stage.scrollLeft,
    top: stage.scrollTop,
  }
  stage.classList.add("dragging")
  stage.setPointerCapture(e.pointerId)
})
$("#viewer-stage").addEventListener("pointermove", e => {
  if (!viewerDrag) return
  const stage = $("#viewer-stage")
  stage.scrollLeft = viewerDrag.left - e.clientX + viewerDrag.x
  stage.scrollTop = viewerDrag.top - e.clientY + viewerDrag.y
})
$("#viewer-stage").addEventListener("pointerup", e => {
  viewerDrag = null
  $("#viewer-stage").classList.remove("dragging")
  $("#viewer-stage").releasePointerCapture(e.pointerId)
})
$("#viewer-stage").addEventListener("pointercancel", () => {
  viewerDrag = null
  $("#viewer-stage").classList.remove("dragging")
})
window.addEventListener("keydown", e => { if (e.key === "Escape") closeViewer() })

function renderPager(id, data) {
  const pager = $("#pager")
  const prev = Math.max(data.page - 1, 1)
  const next = Math.min(data.page + 1, data.pages)
  const start = data.total ? (data.page - 1) * PAGE_SIZE + 1 : 0
  const end = Math.min(data.page * PAGE_SIZE, data.total)
  pager.innerHTML = `<button class="btn-secondary" id="page-prev" ${data.page <= 1 ? "disabled" : ""}>Prev</button>
    <span>${start}-${end} of ${data.total} | page ${data.page}/${data.pages}</span>
    <button class="btn-secondary" id="page-next" ${data.page >= data.pages ? "disabled" : ""}>Next</button>`
  $("#page-prev").onclick = () => { location.hash = explorerHash(id, prev) }
  $("#page-next").onclick = () => { location.hash = explorerHash(id, next) }
}

function esc(s) {
  const d = document.createElement("div")
  d.textContent = s
  return d.innerHTML
}

async function deleteSearch(id) {
  if (!confirm(`Delete search "${id}"?`)) return
  await fetch(`/api/search/${id}`, { method: "DELETE" })
  location.hash = "#/explorer"
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
  location.hash = "#/explorer"
  loadSearches()
}

window.addEventListener("hashchange", route)
if (!location.hash) location.hash = "#/submit"
route()
