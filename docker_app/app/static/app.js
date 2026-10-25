// thumb 单档沿用 small_thumb_url 兼容字段；与 app/services/thumb_chain.py 保持一致。
const THUMB_LEVELS = ["small_thumb_url", "cover_url"];
const THUMB_PROMOTED_LEVELS = ["small_thumb_url", "cover_url"];
function pickThumbUrl(sources, levels = THUMB_LEVELS, originalLast = false) {
  const list = sources.filter(Boolean);
  for (const level of levels) {
    for (const source of list) {
      const value = source[level];
      if (value) return String(value);
    }
  }
  if (originalLast) {
    for (const source of list) {
      if (source.url) return String(source.url);
    }
  }
  return "";
}

// 界面配色的前端镜像：算法与 app/theme.py 保持一致，只负责设置页的实时预览，
// 真正生效的色板仍由服务端 /theme/*.css 派生。预览有偏差也不会影响最终样式。
const UI_THEME_DEFAULTS = { hue: 337, saturation: 0.5, value: 1 };
const UI_THEME_BOUNDS = { hue: [0, 359.9], saturation: [0, 1.5], value: [0.55, 1.45] };
// 基准色板取自 liquid-glass.css 的 :root（它在 app.css 之后加载，同名变量以它为准）。
const UI_THEME_BASE_COLORS = {
  text: "#172842",
  muted: "#65758e",
  accent: "#426ee3",
  accent2: "#199caa",
  accentWarm: "#b77e38",
  danger: "#c74260",
  line: "#465f88",
  surface: "#f5f8fd",
  page: "#eaf0f8",
};
const UI_THEME_ENGINE = {
  HUE_MIN: 170,
  HUE_MAX: 292,
  ANCHOR_HUE: 219,
  HUE_RATIO: 0.4,
  MIN_CHANNEL_DELTA: 2,
  MIN_SATURATION: 0.006,
  LUMA_ITERATIONS: 22,
  clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  },
  hexToRgb(hex) {
    let digits = String(hex || "").trim().replace("#", "");
    if (digits.length === 3) {
      digits = digits.split("").map((char) => char + char).join("");
    }
    if (!/^[0-9a-fA-F]{6}$/.test(digits)) {
      return null;
    }
    return [
      parseInt(digits.slice(0, 2), 16),
      parseInt(digits.slice(2, 4), 16),
      parseInt(digits.slice(4, 6), 16),
    ];
  },
  rgbToHex(rgb) {
    return (
      "#" +
      rgb
        .map((channel) =>
          Math.round(this.clamp(channel, 0, 255))
            .toString(16)
            .padStart(2, "0"),
        )
        .join("")
    );
  },
  rgba(hex, alpha) {
    const rgb = this.hexToRgb(hex) || [0, 0, 0];
    return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
  },
  channelToLinear(channel) {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
  },
  relativeLuma(rgb) {
    return (
      0.2126 * this.channelToLinear(rgb[0]) +
      0.7152 * this.channelToLinear(rgb[1]) +
      0.0722 * this.channelToLinear(rgb[2])
    );
  },
  rgbToHsv(rgb) {
    const red = rgb[0] / 255;
    const green = rgb[1] / 255;
    const blue = rgb[2] / 255;
    const max = Math.max(red, green, blue);
    const min = Math.min(red, green, blue);
    const delta = max - min;
    let hue = 0;
    if (delta !== 0) {
      if (max === red) {
        hue = ((green - blue) / delta) % 6;
      } else if (max === green) {
        hue = (blue - red) / delta + 2;
      } else {
        hue = (red - green) / delta + 4;
      }
      hue /= 6;
      if (hue < 0) {
        hue += 1;
      }
    }
    return [hue * 360, max === 0 ? 0 : delta / max, max];
  },
  hsvToRgb(hue, saturation, value) {
    const h = (((hue % 360) + 360) % 360) / 360;
    const floor = Math.floor(h * 6);
    const sector = floor % 6;
    const f = h * 6 - floor;
    const p = value * (1 - saturation);
    const q = value * (1 - f * saturation);
    const t = value * (1 - (1 - f) * saturation);
    const table = [
      [value, t, p],
      [q, value, p],
      [p, value, t],
      [p, q, value],
      [t, p, value],
      [value, p, q],
    ];
    return table[sector].map((channel) => Math.round(this.clamp(channel, 0, 1) * 255));
  },
  valueForLuma(hue, saturation, target) {
    if (this.relativeLuma(this.hsvToRgb(hue, saturation, 1)) <= target) {
      return 1;
    }
    if (this.relativeLuma(this.hsvToRgb(hue, saturation, 0)) >= target) {
      return 0;
    }
    let low = 0;
    let high = 1;
    for (let index = 0; index < this.LUMA_ITERATIONS; index += 1) {
      const middle = (low + high) / 2;
      if (this.relativeLuma(this.hsvToRgb(hue, saturation, middle)) < target) {
        low = middle;
      } else {
        high = middle;
      }
    }
    return (low + high) / 2;
  },
  transformRgb(rgb, theme) {
    if (Math.max(...rgb) - Math.min(...rgb) < this.MIN_CHANNEL_DELTA) {
      return rgb;
    }
    const [hue, saturation] = this.rgbToHsv(rgb);
    if (saturation < this.MIN_SATURATION || hue < this.HUE_MIN || hue > this.HUE_MAX) {
      return rgb;
    }
    const targetHue = (theme.hue + (hue - this.ANCHOR_HUE) * this.HUE_RATIO) % 360;
    const targetSaturation = this.clamp(saturation * theme.saturation, 0, 1);
    const targetValue = this.clamp(
      this.valueForLuma(targetHue, targetSaturation, this.relativeLuma(rgb)) * theme.value,
      0,
      1,
    );
    return this.hsvToRgb(targetHue, targetSaturation, targetValue);
  },
  transformHex(hex, theme) {
    const rgb = this.hexToRgb(hex);
    if (!rgb) {
      return hex;
    }
    return this.rgbToHex(this.transformRgb(rgb, theme));
  },
  contrast(foreground, background) {
    const first = this.relativeLuma(this.hexToRgb(foreground) || [255, 255, 255]);
    const second = this.relativeLuma(this.hexToRgb(background) || [0, 0, 0]);
    const lighter = Math.max(first, second);
    const darker = Math.min(first, second);
    return (lighter + 0.05) / (darker + 0.05);
  },
  normalize(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const theme = {};
    for (const key of ["hue", "saturation", "value"]) {
      const bound = UI_THEME_BOUNDS[key];
      const rawValue = source[key];
      if (rawValue === undefined || rawValue === null || rawValue === "") {
        theme[key] = UI_THEME_DEFAULTS[key];
        continue;
      }
      const value = Number(rawValue);
      theme[key] = Number.isFinite(value)
        ? Math.round(this.clamp(value, bound[0], bound[1]) * 1000) / 1000
        : UI_THEME_DEFAULTS[key];
    }
    return theme;
  },
  revision(theme) {
    return `h${theme.hue}s${theme.saturation}v${theme.value}`;
  },
};

function galleryApp(initialSettings = {}) {
  const defaultRandom = initialSettings.gallery_default_random === true;
  const defaultSortMode = defaultRandom ? "random" : "imported";
  // Keep native Window objects outside Alpine's reactive proxies.
  let masonryObserver = null;
  const masonryElements = new Set();
  let detailResizeObserver = null;
  let galleryFetchController = null;
  let detailFetchController = null;
  let webkitFullscreenVideo = null;
  let mediaLongPressTimer = null;
  let mediaLongPressStart = null;
  let mediaLongPressOpened = false;
  let mediaMenuAnchor = null;
  let settingsGroupAnimation = null;
  let settingsGroupToken = 0;
  return {
    currentView: "gallery",
    navigationView: "gallery",
    touchBackGesture: null,
    touchBackProgress: 0,
    trackpadBackProgress: 0,
    trackpadBackLayer: "",
    trackpadBackHintTimer: null,
    touchBackClickUntil: 0,
    mediaContextMenu: { open: false, folderName: "", pairIndex: null, x: 12, y: 12 },
    category: "all",
    navItems: [
      { key: "all", label: "全部项目", short: "全", copy: "所有图片与动态内容" },
      { key: "search", label: "搜索", short: "搜", copy: "按标题、正文、订阅源搜索" },
      { key: "favorites", label: "收藏", short: "藏", copy: "只看已收藏动态" },
      { key: "livephoto", label: "Live Photo", short: "动", copy: "只看可播放动态" },
      { key: "video", label: "视频", short: "视", copy: "独立视频，按当前排序连续观看" },
    ],
    meta: { counts: {}, years: {} },
    sidebarCounts: {},
    sidebarCountUpdatedAt: {},
    subscriptions: [],
    latestSubscriptionPullAt: "",
    subscriptionPanel: ["overview", "up", "site", "xhs"].includes(localStorage.getItem("subscription_panel"))
      ? localStorage.getItem("subscription_panel")
      : "overview",
    subscriptionSearch: "",
    subscriptionOverviewSort: localStorage.getItem("subscription_overview_sort") || "name",
    subscriptionOverviewGroup: localStorage.getItem("subscription_overview_group") || "kind",
    selectedSubscriptionUids: [],
    searchDraft: localStorage.getItem("gallery_search_query") || "",
    searchQuery: localStorage.getItem("gallery_search_query") || "",
    searchResultCount: null,
    searchResultSignature: "",
    settingsGroup: "sync",
    settingsBaseline: "",
    settingsSaving: false,
    settingsError: "",
    livephotoSubmitting: false,
    xhsAuthorSubmitting: false,
    trashSearch: "",
    modalFocusStack: [],
    sourceKind: ["all", "up", "site", "xhs"].includes(localStorage.getItem("gallery_source_kind"))
      ? localStorage.getItem("gallery_source_kind")
      : "all",
    sourceKindOptions: [
      { key: "all", label: "所有项目" },
      { key: "up", label: "UP订阅" },
      { key: "site", label: "站点订阅" },
      { key: "xhs", label: "小红书" },
    ],
    searchTags: ["九图", "Live Photo", "收藏", "站点订阅", "COS", "写真", "视频", "转发"],
    gallery: { items: [], total: 0, page: 1, page_size: 36, total_pages: 1 },
    recentUpdates: { groups: [], total_groups: 0, total_items: 0 },
    recentUpdatesLoading: false,
    recentUpdatesPendingRefresh: false,
    recentUpdatesRequestId: 0,
    recentUpdateExpanded: {},
    recentUpdateExpandable: {},
    recentUpdateRowHeights: {},
    recentUpdateMeasureRaf: null,
    galleryRenderStart: 0,
    galleryRenderEnd: 0,
    galleryEstimatedPageHeight: 980,
    galleryWindowShiftRaf: null,
    galleryWindowShiftLock: false,
    galleryWindowPageCount: 3,
    galleryWindowMeasureRaf: null,
    galleryLoading: false,
    galleryFetching: false,
    galleryRandomSeed: /^[A-Za-z0-9_-]{1,128}$/.test(localStorage.getItem("gallery_random_seed") || "")
      ? localStorage.getItem("gallery_random_seed") : "",
    galleryRandomExpired: false,
    randomPullGesture: null,
    randomPullProgress: 0,
    randomRefreshing: false,
    galleryMinHeight: 0,
    galleryViewMode: "folder",
    displayedGalleryViewMode: "folder",
    sortOrder: defaultSortMode,
    timeSortMode: defaultSortMode,
    orderedSortMode: "imported",
    galleryRenderKey: 0,
    galleryPayloadFingerprint: "",
    galleryRequestId: 0,
    galleryRenderToken: 0,
    galleryBatchTimer: null,
    galleryBatchQueue: [],
    gallerySkeletonCount: 0,
    galleryCardQuality: {},
    galleryQualityUpdateQueue: {},
    galleryQualityUpdateRaf: null,
    gallerySmallObserver: null,
    galleryObservedCardElements: {},
    galleryLoadObserver: null,
    galleryLoadTimer: null,
    galleryScrollSettling: false,
    galleryScrollSettleTimer: null,
    gallerySmallPromotionTimer: null,
    galleryColumnCache: null,
    galleryColumnCacheWidth: 0,
    galleryStageWidth: 0,
    detailColumnCache: null,
    detailColumnCacheWidth: 0,
    deferredGalleryRefreshTimer: null,
    interactionActive: false,
    interactionTimer: null,
    idleTaskHandles: {},
    nextIdleTaskId: 1,
    galleryColumnCount: (() => {
      const stored = Number(localStorage.getItem("gallery_column_count"));
      if (Number.isFinite(stored) && stored >= 1) {
        return stored;
      }
      const legacy = Number(localStorage.getItem("gallery_thumb_size") || 280);
      if (legacy >= 360) return 3;
      if (legacy >= 300) return 4;
      if (legacy >= 240) return 5;
      return 6;
    })(),
    detailColumnCount: (() => {
      const stored = Number(localStorage.getItem("detail_column_count"));
      if (Number.isFinite(stored) && stored >= 1) {
        return stored;
      }
      const legacy = Number(localStorage.getItem("detail_thumb_size") || 180);
      if (legacy >= 300) return 2;
      if (legacy >= 240) return 3;
      if (legacy >= 190) return 4;
      return 5;
    })(),
    detailRenderStart: 0,
    detailRenderEnd: 0,
    detailEstimatedRowHeight: 270,
    detailGridGap: 14,
    detailWindowRowCount: 10,
    detailWindowShiftRaf: null,
    detailWindowShiftLock: false,
    detailHeaderCompact: false,
    detailLoading: false,
    detailError: "",
    detailRequestId: 0,
    detailClosing: false,
    detailCloseTimer: null,
    detailTextExpanded: false,
    detailTextOverflowing: false,
    viewerPreviewRetired: false,
    viewerPreviewRetireTimer: null,
    viewerClosing: false,
    viewerCloseTimer: null,
    headerCompact: false,
    headerScrollRaf: null,
    layoutResizeObserver: null,
    layoutMetricRaf: null,
    layoutTopbarReserve: 0,
    lastScrollTop: 0,
    scrollDirection: "down",
    reviewItems: [],
    reviewRemovingIds: {},
    reviewActionId: null,
    duplicateGroups: [],
    duplicateLoading: false,
    duplicateImageThreshold: 2,
    duplicateHasCache: false,
    duplicateCacheStale: false,
    duplicateCacheUpdatedAt: null,
    duplicatePendingActions: {},
    duplicateIgnoreConfirmSignature: null,
    duplicateCleanupConfirmSignature: null,
    duplicateKeepSelections: {},
    logs: [],
    taskRuns: [],
    queuedTasks: [],
    trashItems: [],
    trashGroupExpanded: {},
    siteStats: {},
    siteStatus: {},
    siteSources: [],
    xhsSources: [],
    xhsDrafts: {},
    xhsBusy: false,
    xhsResetSteps: {},
    xhsNewSource: { name: "我的小红书点赞", owner_key: "", anchor_count: 3, initial_fetch_limit: 100, refresh_interval_minutes: 720, refresh_enabled: false },
    xhsCreateExpanded: false,
    xhsNotes: {},
    xhsWorker: { state: 'NOT_CONFIGURED', owner_key: null },
    xhsAutomaticAvailable: false,
    xhsLoginSession: null,
    xhsLoginTimer: null,
    xhsLoginStarting: false,
    xhsLoginRevision: 0,
    xhsBrowserVisible: false,
    xhsQr: null,
    xhsQrTimer: null,
    xhsQrBusy: false,
    xhsQrErrors: 0,
    xhsQrFinishing: false,
    xhsQrAutoFinishTried: false,
    xhsQrFinishRetryAt: 0,
    xhsChecks: {},
    siteLogs: [],
    siteRules: { mode: "blacklist", keywords: [], allow_keywords: [], block_keywords: [], use_regex: false },
    siteRuleText: {
      allow_keywords: "",
      block_keywords: "",
      title_allow: "",
      title_block: "",
      tag_allow: "",
      tag_block: "",
    },
    sitePreviewItems: [],
    siteTestLoading: false,
    siteSuggestLoading: false,
    siteSuggestion: null,
    siteSourceForm: {},
    newSiteSourceExpanded: false,
    siteSourceFormExpanded: false,
    siteSourceExpanded: {},
    siteSourceDrafts: {},
    sitePreviewItemsById: {},
    siteTestLoadingById: {},
    siteSourceSaving: false,
    siteSourceSavingById: {},
    siteIconRefreshingById: {},
    iconRefreshVersions: {},
    siteRulesExpanded: false,
    siteLogsExpanded: false,
    siteLogsClearConfirm: false,
    siteValidateConfirmId: null,
    siteValidateConfirmStep: 0,
    siteValidatePageDraftById: {},
    siteClearDeleteConfirmId: null,
    siteClearDeleteConfirmStep: 0,
    settings: { gallery_default_random: defaultRandom, ...initialSettings },
    savedGalleryDefaultRandom: defaultRandom,
    performanceMode: ["auto", "full", "low"].includes(localStorage.getItem("gallery_performance_mode"))
      ? localStorage.getItem("gallery_performance_mode")
      : "auto",
    resolvedPerformanceMode: "full",
    performanceSamples: [],
    performanceSampleTimer: null,
    galleryIndexStatus: {},
    dataUpgrade: {},
    dataUpgradeSubmitting: false,
    dataCheckReport: null,
    dataCheckReportPage: 1,
    storageStats: {},
    storageStatsLoading: false,
    storageCleanupRunning: false,
    newSubscriptionUid: "",
    keywordText: "",
    pullStatus: {},
    qr: {},
    detail: { open: false, pairs: [], folder: null, videos: [] },
    videoNote: { open: false, loading: false, folder: null, video: null, error: '' },
    detailCache: {},
    detailCacheOrder: [],
    detailCacheLimit: 3,
    viewer: { open: false, pair: null, folder: null, showVideo: false },
    viewerSource: "detail",
    viewerSequence: [],
    viewerIndex: 0,
    viewerToken: 0,
    viewerImageReady: false,
    viewerImageLoading: false,
    viewerImageProgress: null,
    viewerImageSource: "",
    viewerImageCancel: null,
    viewerImageObjectUrl: "",
    viewerSwipeCancelAnimations: null,
    viewerSwipeFinish: null,
    viewerSwipeTimer: null,
    viewerHandoff: null,
    viewerVerticalDragging: false,
    viewerEdgeCancel: null,
    viewerEdgeWheelTimer: null,
    viewerImageRevealTimer: null,
    viewerImageRevealAfter: 0,
    viewerSwapPending: false,
    viewerPendingDirection: "",
    viewerZoom: 1,
    viewerToolbarOpen: false,
    viewerToolbarHideTimer: null,
    viewerPositionHintVisible: false,
    viewerPositionHintTimer: null,
    viewerOffsetX: 0,
    viewerOffsetY: 0,
    viewerTransitionEnabled: true,
    viewerSwitchDirection: "",
    viewerSwitchTimer: null,
    viewerPlaybackTimer: null,
    viewerDeferredTimer: null,
    viewerClickTimer: null,
    viewerGesture: {
      pointers: {},
      startX: 0,
      startY: 0,
      startOffsetX: 0,
      startOffsetY: 0,
      startZoom: 1,
      startDistance: 0,
      active: false,
      isPinch: false,
    },
    trackpadGesture: null,
    viewerWheelTimer: null,
    viewerNavigationLockedUntil: 0,
    timeFilterOpen: false,
    timeFilterDraft: { startIndex: 0, endIndex: 0 },
    timeFilterApplied: { startMonth: null, endMonth: null },
    timeFilterDrag: { active: false, handle: null },
    timeFilterApplyTimer: null,
    playbackModes: ["loop", "pingpong", "once", "pause"],
    playbackMode: localStorage.getItem("livephoto_playback_mode") || "once",
    loadingMore: false,
    loadingPrevious: false,
    hoverPreviewEnabled: localStorage.getItem("livephoto_hover_preview") !== "0",
    hoverPreviewTimer: null,
    hoverPreviewCard: null,
    hoverPreviewLoadCard: null,
    sidebarCollapsed: localStorage.getItem("gallery_sidebar_collapsed") === "1",
    subscriptionSectionCollapsed: true,
    narrowViewport: false,
    videoFeed: { active: false, items: [], index: -1, page: 1, totalPages: 1, params: '', busy: false },
    videoFeedToken: 0,
    videoFeedGesture: null,
    videoFeedMessage: '',
    videoFeedHintVisible: false,
    videoFeedHintTimer: null,
    videoFeedDirection: 1,
    videoFeedPending: null,
    videoFeedRetiring: null,
    videoFeedSwitching: false,
    videoFeedReadyTimer: null,
    videoFeedRetireTimer: null,
    videoFeedPositionTotal: null,
    videoNoteFullscreen: false,
    videoNoteClosing: false,
    videoNoteCloseTimer: null,
    subscriptionPullPending: false,
    allSubscriptionsPullPending: false,
    compactViewport: false,
    sidebarDrawerOpen: false,
    favoritePending: {},
    pendingTrashFolder: null,
    pendingRestoreTrashId: null,
    clearDataConfirmStep: 0,
    validateConfirmStep: 0,
    validateActionsRevealed: false,
    rebuildIndexConfirmStep: 0,
    thumbnailRebuildConfirmLevel: null,
    resetSubscriptionIconsConfirmStep: 0,
    resetSubscriptionIconsRunning: false,
    resetSiteIconsConfirmStep: 0,
    resetSiteIconsRunning: false,
    resetXhsIconsConfirmStep: 0,
    resetXhsIconsRunning: false,
    themeDraft: { ...UI_THEME_DEFAULTS },
    themeSaved: { ...UI_THEME_DEFAULTS },
    themeSavedRevision: "",
    themeDefaults: { ...UI_THEME_DEFAULTS },
    themeBounds: {
      hue: [...UI_THEME_BOUNDS.hue],
      saturation: [...UI_THEME_BOUNDS.saturation],
      value: [...UI_THEME_BOUNDS.value],
    },
    themeSwatches: {},
    themeContrastRatio: 4.88,
    themeSaving: false,
    themeResetConfirmStep: 0,
    fullReloadConfirmStep: 0,
    clearTaskLogsConfirmStep: 0,
    clearFilterLogsConfirmStep: 0,
    subscriptionReloadConfirmUid: null,
    subscriptionReloadConfirmStep: 0,
    subscriptionDeleteConfirmUid: null,
    subscriptionDeleteConfirmStep: 0,
    subscriptionIconRefreshingUid: null,
    subscriptionExpanded: {},
    iconLoadFailures: {},
    queuedCancelConfirmId: null,
    deletePairConfirmKey: null,
    deletePairConfirmStep: 0,
    pairDeletingKey: null,
    taskInspector: { open: false, title: "", subtitle: "", body: "" },
    sourcePreview: { open: false, url: "", title: "", subtitle: "", reviewId: null },
    taskInspectorLoading: false,
    toast: { open: false, tone: "info", title: "", message: "" },
    toastTimer: null,
    statusRefreshTimer: null,
    lastTaskId: null,
    lastRunning: false,
    taskHistoryExpanded: false,
    viewerSyntheticTapUntil: 0,
    bodyLockTop: null,
    bodyLockPaddingRight: "",
    bodyLockStyles: null,
    deletedGalleryTimers: {},
    galleryReflowTimer: null,
    lazyLoaded: {
      duplicates: false,
      review: false,
      logs: false,
      tasks: false,
      recentUpdates: false,
      trash: false,
      sites: false,
      settings: false,
    },

    async init() {
      const glassNavigationKey = () => JSON.stringify([
        this.currentView,
        this.currentView === "gallery"
          ? [this.category, this.sourceKind, [...this.selectedSubscriptionUids].sort()]
          : this.currentView === "subscriptions" ? this.subscriptionPanel : null,
      ]);
      this.$nextTick(() => window.GalleryGlass?.refresh({ navigationKey: glassNavigationKey() }));
      ["currentView", "category", "subscriptionPanel", "sourceKind", "selectedSubscriptionUids"].forEach((key) => {
        this.$watch(key, () => this.$nextTick(() => window.GalleryGlass?.refresh({
          viewChanged: true,
          navigationKey: glassNavigationKey(),
        })));
      });
      ["compactViewport", "sidebarCollapsed"].forEach((key) => {
        this.$watch(key, () => this.$nextTick(() => window.GalleryGlass?.refresh()));
      });
      this.updateViewportMode();
      this.installTouchNavigation();
      ["videoNote.open", "detail.open", "viewer.open", "taskInspector.open", "sourcePreview.open", "mediaContextMenu.open"].forEach((key) => {
        this.$watch(key, () => this.$nextTick(() => this.syncModalAccessibility()));
      });
      window.addEventListener("beforeunload", (event) => {
        if (this.settingsDirty()) { event.preventDefault(); event.returnValue = ""; }
      });
      this.resetSiteSourceForm();
      window.addEventListener("scroll", () => this.scheduleScrollEffects(), { passive: true });
      window.addEventListener("keydown", (event) => this.handleKeydown(event));
      window.addEventListener("resize", () => this.updateViewportMode(), { passive: true });
      window.addEventListener("orientationchange", () => this.updateViewportMode(), { passive: true });
      document.documentElement.style.setProperty("--sidebar-state", this.sidebarCollapsed ? "collapsed" : "expanded");
      this.$nextTick(() => this.installFixedLayoutObservers());
      window.addEventListener("pointermove", (event) => this.timeFilterPointerMove(event));
      window.addEventListener("pointerup", () => this.finishTimeFilterDrag());
      window.addEventListener("pointercancel", () => this.finishTimeFilterDrag());
      this.updateHeaderState();
      // First paint must not wait for account checks, metadata or subscriptions.
      this.refreshGallery(true).catch(() => {}).finally(() => this.loadBootstrap().catch(() => {}));
      this.scheduleStatusRefresh();
      this.startPerformanceSampling();
      window.setInterval(() => {
        if (this.qr.image_data_url && this.currentView === "settings") {
          this.pollQrStatus();
        }
      }, 3000);
    },

    setInteracting(duration = 320) {
      this.interactionActive = true;
      document.documentElement.classList.add("is-interacting");
      if (this.interactionTimer) {
        window.clearTimeout(this.interactionTimer);
      }
      this.interactionTimer = window.setTimeout(() => {
        this.interactionActive = false;
        document.documentElement.classList.remove("is-interacting");
        this.interactionTimer = null;
      }, duration);
    },

    runAfterInteraction(callback, delay = 260) {
      this.setInteracting(delay + 120);
      return window.setTimeout(() => callback(), delay);
    },

    scheduleIdleWork(callback, timeout = 700) {
      const id = this.nextIdleTaskId;
      this.nextIdleTaskId += 1;
      const wrapped = () => {
        delete this.idleTaskHandles[id];
        callback();
      };
      if (typeof window.requestIdleCallback === "function") {
        this.idleTaskHandles[id] = window.requestIdleCallback(wrapped, { timeout });
      } else {
        this.idleTaskHandles[id] = window.setTimeout(wrapped, Math.min(timeout, 120));
      }
      return id;
    },

    cancelIdleWork(id) {
      const handle = this.idleTaskHandles[id];
      if (!handle) {
        return;
      }
      if (typeof window.cancelIdleCallback === "function") {
        window.cancelIdleCallback(handle);
      } else {
        window.clearTimeout(handle);
      }
      delete this.idleTaskHandles[id];
    },

    deferHeavyUiWork(callback, delay = 220) {
      this.setInteracting(delay + 140);
      return window.setTimeout(() => this.scheduleIdleWork(callback, 900), delay);
    },

    performanceModeLabel() {
      return {
        auto: "自动",
        full: "完整视觉",
        low: "低功耗",
      }[this.performanceMode] || "自动";
    },

    setPerformanceMode(mode) {
      if (!["auto", "full", "low"].includes(mode)) {
        return;
      }
      this.performanceMode = mode;
      localStorage.setItem("gallery_performance_mode", mode);
      this.resolvePerformanceMode();
    },

    shouldUseLowPerformanceMode(frameTimes = []) {
      const viewportPixels = (window.innerWidth || 1280)
        * (window.innerHeight || 720)
        * Math.max(1, Number(window.devicePixelRatio) || 1) ** 2;
      const deviceMemory = Number(window.navigator?.deviceMemory || 0);
      const sorted = [...frameTimes].sort((left, right) => left - right);
      const p95 = sorted.length ? sorted[Math.floor(sorted.length * 0.95)] : 0;
      return viewportPixels >= 4_000_000 || (deviceMemory > 0 && deviceMemory <= 8) || p95 > 22;
    },

    resolvePerformanceMode(frameTimes = this.performanceSamples) {
      const next = this.performanceMode === "auto"
        ? (this.shouldUseLowPerformanceMode(frameTimes) ? "low" : "full")
        : this.performanceMode;
      this.resolvedPerformanceMode = next;
      document.documentElement.dataset.performanceMode = next;
    },

    startPerformanceSampling() {
      this.resolvePerformanceMode();
      if (this.performanceMode !== "auto" || typeof window.requestAnimationFrame !== "function") {
        return;
      }
      const frameTimes = [];
      let previous = 0;
      let samples = 0;
      const sample = (now) => {
        if (previous) {
          frameTimes.push(now - previous);
        }
        previous = now;
        samples += 1;
        if (samples >= 45) {
          this.performanceSamples = frameTimes.slice(-45);
          this.resolvePerformanceMode(this.performanceSamples);
          this.performanceSampleTimer = null;
          return;
        }
        window.requestAnimationFrame(sample);
      };
      this.performanceSampleTimer = window.setTimeout(() => window.requestAnimationFrame(sample), 900);
    },

    scheduleDeferredGalleryRefresh(reset = true, delay = 220) {
      if (this.deferredGalleryRefreshTimer) {
        window.clearTimeout(this.deferredGalleryRefreshTimer);
      }
      this.setInteracting(delay + 180);
      this.gallerySkeletonCount = reset ? this.visibleGalleryBatchSize() : this.gallerySkeletonCount;
      this.deferredGalleryRefreshTimer = window.setTimeout(() => {
        this.deferredGalleryRefreshTimer = null;
        this.refreshGallery(reset).catch(() => {});
      }, delay);
    },

    refreshGalleryImmediately(reset = true) {
      if (this.deferredGalleryRefreshTimer) {
        window.clearTimeout(this.deferredGalleryRefreshTimer);
        this.deferredGalleryRefreshTimer = null;
      }
      if (reset) {
        // 不再预先清空 items 与 galleryCardQuality：数据未变时旧卡片原地保留、零重绘；
        // 数据变化时由 applyGalleryPagePayload 一次性替换。先清空只会让所有卡片
        // 销毁重建，入场动画、渐进图淡入与质量预热整体重放（切页闪烁的来源）。
        this.galleryEstimatedPageHeight = 980;
        this.gallery = {
          ...this.gallery,
          page: 1,
          total_pages: 1,
          view_mode: this.galleryViewMode,
        };
        this.displayedGalleryViewMode = this.galleryViewMode;
      }
      return this.refreshGallery(reset).catch(() => {});
    },

    scheduleScrollEffects() {
      if (this.bodyLockTop !== null) {
        this.lastScrollTop = this.bodyLockTop;
        return;
      }
      const currentScrollTop = window.scrollY || window.pageYOffset || 0;
      if (this.currentView === "gallery" && !this.detail.open && !this.viewer.open) {
        this.markGalleryScrollActive();
      }
      if (currentScrollTop > this.lastScrollTop + 2) {
        this.scrollDirection = "down";
      } else if (currentScrollTop < this.lastScrollTop - 2) {
        this.scrollDirection = "up";
      }
      this.lastScrollTop = currentScrollTop;
      if (this.headerScrollRaf) {
        return;
      }
      this.headerScrollRaf = window.requestAnimationFrame(() => {
        this.headerScrollRaf = null;
        this.updateHeaderState(this.lastScrollTop);
        this.scheduleGalleryWindowShift();
        this.handleScroll();
      });
    },

    markGalleryScrollActive() {
      this.setInteracting(240);
      this.galleryScrollSettling = true;
      if (this.galleryScrollSettleTimer) {
        window.clearTimeout(this.galleryScrollSettleTimer);
      }
      this.galleryScrollSettleTimer = window.setTimeout(() => {
        this.galleryScrollSettling = false;
        this.galleryScrollSettleTimer = null;
        this.recalculateGalleryExposure();
      }, 180);
    },

    recalculateGalleryExposure() {
      this.promoteVisibleGallerySmall();
      this.scheduleGalleryWindowShift();
      this.handleScroll();
    },

    galleryWindowPageSize() {
      return Math.max(1, Number(this.gallery.page_size || 36));
    },

    galleryWindowSize() {
      return this.galleryWindowPageSize() * this.galleryWindowPageCount;
    },

    galleryWindowItems() {
      const items = this.gallery.items || [];
      const start = Math.max(0, Math.min(this.galleryRenderStart || 0, Math.max(0, items.length - 1)));
      const end = Math.min(items.length, Math.max(start, this.galleryRenderEnd || this.galleryWindowSize()));
      return items.slice(start, end);
    },

    galleryVirtualSpacerStyle(position) {
      const items = this.gallery.items || [];
      const pageSize = this.galleryWindowPageSize();
      const start = Math.max(0, this.galleryRenderStart || 0);
      const end = Math.min(items.length, Math.max(start, this.galleryRenderEnd || this.galleryWindowSize()));
      const pagesBefore = start / pageSize;
      const pagesAfter = Math.max(0, items.length - end) / pageSize;
      const height = (position === "top" ? pagesBefore : pagesAfter) * Math.max(320, this.galleryEstimatedPageHeight || 980);
      return height > 0 ? `height: ${Math.round(height)}px;` : "display: none;";
    },

    galleryWindowAnchor() {
      const element = document.querySelector(".gallery-item-card[data-gallery-key]");
      if (!element) {
        return null;
      }
      return {
        key: element.getAttribute("data-gallery-key"),
        top: element.getBoundingClientRect().top,
      };
    },

    measureGalleryWindow() {
      const element = document.querySelector(".gallery-page");
      if (!element) {
        return;
      }
      const renderedCount = this.galleryWindowItems().length;
      const pageSize = this.galleryWindowPageSize();
      const pageCount = Math.max(1, Math.ceil(renderedCount / pageSize));
      const height = Number(element.getBoundingClientRect().height || 0) / pageCount;
      if (Number.isFinite(height) && height > 160) {
        this.galleryEstimatedPageHeight = Math.round(Math.max(320, Math.min(20000, height)));
      }
    },

    pruneGalleryWindowCaches() {
      const keys = new Set(this.galleryWindowItems().map((item) => this.galleryItemKey(item)).filter(Boolean));
      this.galleryCardQuality = Object.fromEntries(Object.entries(this.galleryCardQuality || {})
        .filter(([key], index, entries) => keys.has(key) || index >= entries.length - 512));
      this.galleryQualityUpdateQueue = Object.fromEntries(Object.entries(this.galleryQualityUpdateQueue || {})
        .filter(([key]) => keys.has(key)));
    },

    scheduleGalleryWindowShift() {
      if (this.galleryWindowShiftRaf || this.galleryWindowShiftLock || (this.gallery.items || []).length <= this.galleryWindowSize()) {
        return;
      }
      this.galleryWindowShiftRaf = window.requestAnimationFrame(() => {
        this.galleryWindowShiftRaf = null;
        this.shiftGalleryWindowIfNeeded();
      });
    },

    shiftGalleryWindowIfNeeded() {
      if (this.galleryWindowShiftLock || this.currentView !== "gallery" || this.detail.open || this.viewer.open) {
        return;
      }
      const items = this.gallery.items || [];
      const pageSize = this.galleryWindowPageSize();
      const windowSize = this.galleryWindowSize();
      const start = Math.max(0, this.galleryRenderStart || 0);
      const end = Math.min(items.length, Math.max(start, this.galleryRenderEnd || windowSize));
      const cards = Array.from(document.querySelectorAll(".gallery-item-card[data-gallery-key]"));
      if (!cards.length) {
        return;
      }
      const first = cards[0].getBoundingClientRect();
      const last = cards[cards.length - 1].getBoundingClientRect();
      const preload = this.galleryPreloadDistance();
      let nextStart = start;
      let direction = "";
      const viewportMissesWindow = first.top > window.innerHeight + preload || last.bottom < -preload;
      const atTop = (window.scrollY || 0) <= 2;
      if (atTop && start > 0) {
        nextStart = 0;
        direction = "previous";
      } else if (viewportMissesWindow) {
        const virtual = document.querySelector(".gallery-virtual-page");
        const offset = virtual ? Math.max(0, -virtual.getBoundingClientRect().top) : 0;
        const targetPage = Math.floor(offset / Math.max(320, this.galleryEstimatedPageHeight));
        nextStart = Math.min(Math.max(0, (targetPage - 1) * pageSize), Math.max(0, items.length - windowSize));
        direction = nextStart > start ? "next" : "previous";
      } else if (this.scrollDirection === "down" && end < items.length && last.bottom <= window.innerHeight + preload) {
        nextStart = Math.min(start + pageSize, Math.max(0, items.length - windowSize));
        direction = "next";
      } else if (this.scrollDirection === "up" && start > 0 && first.top >= -preload) {
        nextStart = Math.max(0, start - pageSize);
        direction = "previous";
      }
      if (!direction || nextStart === start) {
        return;
      }
      const anchorItem = viewportMissesWindow || atTop ? null : items[direction === "next" ? nextStart : start];
      const anchorKey = this.galleryItemKey(anchorItem);
      const anchorElement = anchorKey
        ? document.querySelector(`[data-gallery-key="${this.cssEscape(anchorKey)}"]`)
        : null;
      const anchorTop = anchorElement ? anchorElement.getBoundingClientRect().top : null;
      this.galleryWindowShiftLock = true;
      this.galleryRenderStart = nextStart;
      this.galleryRenderEnd = Math.min(items.length, nextStart + windowSize);
      this.pruneGalleryWindowCaches();
      this.$nextTick(() => {
        this.measureGalleryWindow();
        this.observeGalleryCards();
        this.observeGalleryLoadSentinel();
        const nextAnchor = anchorKey
          ? document.querySelector(`[data-gallery-key="${this.cssEscape(anchorKey)}"]`)
          : null;
        if (nextAnchor && anchorTop !== null) {
          const delta = nextAnchor.getBoundingClientRect().top - anchorTop;
          if (Math.abs(delta) > 1) {
            window.scrollBy({ top: delta, left: 0, behavior: "instant" });
          }
        }
        window.setTimeout(() => {
          this.galleryWindowShiftLock = false;
        }, 80);
      });
    },

    toggleSidebar() {
      if (this.compactViewport) {
        this.sidebarDrawerOpen = !this.sidebarDrawerOpen;
        this.$nextTick(() => this.scheduleRecentUpdatesMeasure());
        return;
      }
      this.sidebarCollapsed = !this.sidebarCollapsed;
      localStorage.setItem("gallery_sidebar_collapsed", this.sidebarCollapsed ? "1" : "0");
      document.documentElement.style.setProperty("--sidebar-state", this.sidebarCollapsed ? "collapsed" : "expanded");
      this.$nextTick(() => {
        this.scheduleFixedLayoutMetrics();
        this.scheduleRecentUpdatesMeasure();
      });
    },

    updateViewportMode() {
      const nextCompact = window.matchMedia("(max-width: 1100px), (max-aspect-ratio: 11/10)").matches;
      this.compactViewport = nextCompact;
      this.narrowViewport = window.matchMedia("(max-width: 760px), (max-width: 1100px) and (orientation: portrait)").matches;
      if (!nextCompact) {
        this.sidebarDrawerOpen = false;
        this.closeMediaContextMenu();
      }
      this.galleryColumnCache = null;
      this.detailColumnCache = null;
      this.galleryStageWidth = this.$refs?.galleryStage?.clientWidth || window.innerWidth;
      this.$nextTick(() => {
        this.galleryStageWidth = this.$refs?.galleryStage?.clientWidth || window.innerWidth;
        this.observeGalleryCards();
        this.observeGalleryLoadSentinel();
        this.scheduleFixedLayoutMetrics();
        this.refreshDetailTextOverflow();
        this.scheduleRecentUpdatesMeasure();
        this.scheduleDetailWindowShift();
      });
    },

    closeSidebarDrawer() {
      this.sidebarDrawerOpen = false;
    },

    touchBackContextKey() {
      // Only invalidate a gesture when its context changes; pages are never back-history entries.
      return JSON.stringify([this.currentView, this.category, this.sourceKind, this.selectedSubscriptionUids,
        this.subscriptionPanel, this.settingsGroup, this.detailRequestId, this.viewerToken,
        this.videoFeedToken, this.sourcePreview.url]);
    },

    currentFullscreenElement() {
      return document.fullscreenElement || document.webkitFullscreenElement
        || (webkitFullscreenVideo?.webkitDisplayingFullscreen ? webkitFullscreenVideo : null)
        || (this.currentVideoNotePlayer()?.webkitDisplayingFullscreen ? this.currentVideoNotePlayer() : null);
    },

    exitCurrentFullscreen() {
      const element = this.currentFullscreenElement();
      if (!element) return;
      // Exit the native top layer without closing the player or browser underneath it.
      try {
        const result = document.fullscreenElement ? document.exitFullscreen()
          : document.webkitFullscreenElement ? document.webkitExitFullscreen()
          : element.webkitExitFullscreen?.();
        result?.catch?.(() => {});
      } catch (error) { void error; }
    },

    topOverlayLayer(includeClosing = true) {
      if (this.currentFullscreenElement()) return "fullscreen";
      if (this.mediaContextMenu.open) return "media-menu";
      if (this.viewer.open || (includeClosing && this.viewerClosing)) return "viewer";
      if (this.sourcePreview.open) return "source";
      if (this.videoNote.open || (includeClosing && this.videoNoteClosing)) return "video-note";
      if (this.taskInspector.open) return "task";
      if (this.detail.open || (includeClosing && this.detailClosing)) return "detail";
      return "";
    },

    touchBackLayer({ keyboard = false } = {}) {
      const layer = this.topOverlayLayer();
      // The viewer handles its own image navigation, zooming and horizontal actions.
      if (layer === "viewer") return keyboard && this.viewer.open ? "viewer" : "";
      if (layer === "detail" && this.detailClosing) return "";
      if (layer === "video-note" && this.videoNoteClosing) return "";
      if (layer) return layer;
      if (this.sidebarDrawerOpen) return "sidebar";
      if (this.compactViewport) return "open-sidebar";
      return this.sidebarCollapsed ? "expand-sidebar" : "";
    },

    navigateTouchBack(options = {}) {
      const layer = this.touchBackLayer(options);
      if (layer === "fullscreen") { this.exitCurrentFullscreen(); return; }
      if (layer === "media-menu") { this.closeMediaContextMenu(); return; }
      if (layer === "viewer") { this.closeViewer(); return; }
      if (layer === "open-sidebar") { this.sidebarDrawerOpen = true; return; }
      if (layer === "expand-sidebar") { this.toggleSidebar(); return; }
      if (layer === "source") { this.closeSourcePreview(); return; }
      if (layer === "video-note") { this.closeVideoNote(); return; }
      if (layer === "task") { this.closeTaskInspector(); return; }
      if (layer === "detail") { this.closeDetail(); return; }
      if (layer === "sidebar") { this.closeSidebarDrawer(); return; }
    },

    installTouchNavigation() {
      this.installMediaContextMenu();
      document.addEventListener("wheel", event => this.compactTrackpadWheel(event), { capture: true, passive: false });
      document.addEventListener("touchstart", event => this.startRandomPull(event), { capture: true, passive: true });
      document.addEventListener("touchmove", event => this.moveRandomPull(event), { capture: true, passive: false });
      document.addEventListener("touchend", event => this.endRandomPull(event), { capture: true, passive: false });
      document.addEventListener("touchcancel", () => this.cancelRandomPull(), { capture: true, passive: true });
      const fullscreenChanged = () => {
        this.videoNoteFullscreen = !!this.currentFullscreenElement()?.classList?.contains('xhs-video-shell');
        this.cancelTouchBack();
        this.syncModalAccessibility();
      };
      document.addEventListener("fullscreenchange", fullscreenChanged);
      document.addEventListener("webkitfullscreenchange", fullscreenChanged);
      document.addEventListener("webkitbeginfullscreen", event => {
        webkitFullscreenVideo = event.target;
        fullscreenChanged();
      }, true);
      document.addEventListener("webkitendfullscreen", () => {
        webkitFullscreenVideo = null;
        fullscreenChanged();
      }, true);
      // Capture sees gestures even inside drawers that stop touchmove propagation.
      document.addEventListener("touchstart", event => this.startTouchBack(event), { capture: true, passive: true });
      document.addEventListener("touchmove", event => this.moveTouchBack(event), { capture: true, passive: false });
      document.addEventListener("touchend", event => this.endTouchBack(event), { capture: true, passive: false });
      document.addEventListener("touchcancel", () => this.cancelTouchBack(), { capture: true, passive: true });
      document.addEventListener("click", event => {
        if (Date.now() >= this.touchBackClickUntil || event.detail === 0 || event.target?.closest?.(".media-context-menu")) return;
        event.preventDefault();
        event.stopImmediatePropagation();
      }, true);
    },

    wheelGestureStep(event, field, context) {
      const now = Date.now();
      let gesture = this[field];
      if (!gesture || now - gesture.lastAt > (gesture.backCommitted ? 600 : 240)) {
        gesture = { x: 0, y: 0, axis: "", fired: false, context, lastAt: now };
        this[field] = gesture;
      }
      gesture.lastAt = now;
      // Consume the remainder of a burst even if its action changed the active layer.
      if (gesture.context !== context) { gesture.fired = true; return this[field]; }
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? window.innerHeight : 1;
      gesture.x += (event.deltaX || 0) * unit;
      gesture.y += (event.deltaY || 0) * unit;
      if (!gesture.axis && Math.max(Math.abs(gesture.x), Math.abs(gesture.y)) >= 12) {
        if (Math.abs(gesture.x) > Math.abs(gesture.y) * 1.4) gesture.axis = "x";
        else if (Math.abs(gesture.y) > Math.abs(gesture.x) * 1.4) gesture.axis = "y";
      }
      return this[field];
    },

    compactTrackpadWheel(event) {
      if (event.ctrlKey || event.metaKey) return;
      const layer = this.topOverlayLayer();
      if (layer === "viewer") return;
      const target = event.target;
      if (target.closest?.('input, textarea, select, audio, iframe, [contenteditable]:not([contenteditable="false"]), [role="slider"], [data-gesture-ignore]')) return;
      const player = target.closest?.("video");
      if (player && event.clientY > player.getBoundingClientRect().bottom - 72) return;
      for (let element = target; element && element !== document.body; element = element.parentElement) {
        if (element.scrollWidth > element.clientWidth + 2 && /^(auto|scroll)$/.test(window.getComputedStyle(element).overflowX)) return;
      }
      const newBurst = !this.trackpadGesture || Date.now() - this.trackpadGesture.lastAt > 240;
      const gesture = this.wheelGestureStep(event, "trackpadGesture", this.touchBackContextKey());
      if (newBurst) {
        gesture.pullAllowed = this.randomPullAllowed(true);
        for (let element = target; element && element !== document.body; element = element.parentElement) {
          if (element.scrollTop > 2) gesture.pullAllowed = false;
        }
      }
      if (gesture.fired) { if (event.cancelable) event.preventDefault(); return; }
      if (gesture.axis === "x" && this.touchBackLayer()) {
        if (event.cancelable) event.preventDefault();
        this.updateTrackpadBackHint(-gesture.x / this.touchBackThreshold(), this.touchBackLayer());
      } else if (gesture.axis === "y" && layer === "video-note" && this.videoFeed.active
          && !target.closest?.('button, a, header, .video-feed-caption')) {
        if (event.cancelable) event.preventDefault();
        if (Math.abs(gesture.y) >= 72) {
          gesture.fired = true;
          this.stepVideoFeed(gesture.y > 0 ? 1 : -1);
        }
      } else if (gesture.axis === "y" && gesture.y < 0 && gesture.pullAllowed && this.randomPullAllowed(true)
          && target.closest?.(".workspace") && !target.closest?.('button:not(.card-open-button), a, video')) {
        for (let element = target; element && element !== document.body; element = element.parentElement) {
          if (element.scrollTop > 2) return;
        }
        if (event.cancelable) event.preventDefault();
        if (-gesture.y >= this.randomPullThreshold()) {
          gesture.fired = true;
          this.refreshRandomGallery();
        }
      }
    },

    startTouchBack(event) {
      this.cancelTouchBack();
      if (event.touches.length !== 1) return;
      const layer = this.touchBackLayer();
      const target = event.target;
      if (!layer || target.closest?.('input, textarea, select, audio, iframe, [contenteditable]:not([contenteditable="false"]), [role="slider"], [data-gesture-ignore]')
          || window.getSelection()?.toString()) return;
      const touch = event.touches[0];
      const video = target.closest?.("video");
      if (video && touch.clientY > video.getBoundingClientRect().bottom - 72) return;
      for (let element = target; element && element !== document.body; element = element.parentElement) {
        if (element.scrollWidth > element.clientWidth + 2
            && /^(auto|scroll)$/.test(window.getComputedStyle(element).overflowX)) return;
      }
      this.touchBackGesture = { id: touch.identifier, x: touch.clientX, y: touch.clientY,
        startedAt: Date.now(), layer, location: this.touchBackContextKey(), locked: false };
    },

    moveTouchBack(event) {
      const gesture = this.touchBackGesture;
      if (!gesture) return;
      if (event.touches.length !== 1 || this.touchBackLayer() !== gesture.layer
          || this.touchBackContextKey() !== gesture.location) { this.cancelTouchBack(); return; }
      const touch = event.touches[0];
      if (touch.identifier !== gesture.id) { this.cancelTouchBack(); return; }
      const dx = touch.clientX - gesture.x, dy = touch.clientY - gesture.y;
      if (!gesture.locked) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) < 12) return;
        if (dx <= 0 || dx < Math.abs(dy) * 1.4) { this.cancelTouchBack(); return; }
        gesture.locked = true;
      }
      if (event.cancelable) event.preventDefault();
      this.touchBackProgress = Math.max(0, Math.min(1, dx / this.touchBackThreshold()));
    },

    touchBackThreshold() {
      return Math.max(72, Math.min(112, window.innerWidth * 0.22));
    },

    backGestureProgress() {
      return Math.max(this.touchBackProgress, this.trackpadBackProgress);
    },

    backGestureLayer() {
      return this.touchBackGesture?.layer || this.trackpadBackLayer;
    },

    updateTrackpadBackHint(progress, layer) {
      if (this.trackpadBackHintTimer !== null) window.clearTimeout(this.trackpadBackHintTimer);
      this.trackpadBackProgress = Math.max(0, Math.min(1, progress));
      this.trackpadBackLayer = layer;
      const gesture = this.trackpadGesture;
      const context = this.touchBackContextKey();
      this.trackpadBackHintTimer = window.setTimeout(() => {
        this.trackpadBackHintTimer = null;
        // Wheel events expose no finger-up event; finish after the event stream settles.
        if (gesture && gesture === this.trackpadGesture && !gesture.fired
            && context === this.touchBackContextKey() && this.trackpadBackProgress >= 1
            && (layer === "viewer" ? this.viewer.open && this.viewerZoom <= 1.02 : this.touchBackLayer() === layer)) {
          gesture.fired = true;
          gesture.backCommitted = true;
          gesture.lastAt = Date.now();
          if (layer === "viewer") this.closeViewer(); else this.navigateTouchBack();
        }
        this.trackpadBackProgress = 0;
      }, 90);
    },

    endTouchBack(event) {
      const gesture = this.touchBackGesture;
      this.cancelTouchBack();
      if (!gesture || !gesture.locked) return;
      // Also suppress synthetic clicks after an abandoned horizontal drag.
      this.touchBackClickUntil = Date.now() + 450;
      if (event.cancelable) event.preventDefault();
      const touch = Array.from(event.changedTouches).find(item => item.identifier === gesture.id);
      if (!touch || event.touches.length || Date.now() - gesture.startedAt > 1400
          || this.touchBackLayer() !== gesture.layer || this.touchBackContextKey() !== gesture.location) return;
      const dx = touch.clientX - gesture.x, dy = touch.clientY - gesture.y;
      if (dx >= this.touchBackThreshold() && dx > Math.abs(dy) * 1.4) {
        this.videoFeedGesture = null;
        this.navigateTouchBack();
      }
    },

    cancelTouchBack() {
      if (this.touchBackGesture?.layer) this.trackpadBackLayer = this.touchBackGesture.layer;
      this.touchBackGesture = null;
      this.touchBackProgress = 0;
    },

    mediaMenuTarget(target) {
      if (!this.compactViewport || target.closest?.('button:not(.card-open-button):not(.detail-content-toggle), a, input, textarea, select, [contenteditable="true"], [data-gesture-ignore]')) return null;
      const element = target.closest?.("[data-media-menu-folder]");
      return element?.dataset.mediaMenuFolder ? element : null;
    },

    installMediaContextMenu() {
      document.addEventListener("contextmenu", event => {
        const element = this.mediaMenuTarget(event.target);
        if (!element) return;
        event.preventDefault();
        this.openMediaContextMenu(element, event.clientX, event.clientY);
      }, true);
      document.addEventListener("touchstart", event => {
        this.cancelMediaLongPress();
        mediaLongPressOpened = false;
        if (event.touches.length !== 1) return;
        const element = this.mediaMenuTarget(event.target);
        if (!element) return;
        const touch = event.touches[0];
        mediaLongPressStart = { x: touch.clientX, y: touch.clientY };
        mediaLongPressTimer = window.setTimeout(() => {
          mediaLongPressTimer = null;
          if (!element.isConnected || !mediaLongPressStart) return;
          mediaLongPressOpened = true;
          this.openMediaContextMenu(element, touch.clientX, touch.clientY);
        }, 550);
      }, { capture: true, passive: true });
      document.addEventListener("touchmove", event => {
        if (!mediaLongPressStart) return;
        const touch = event.touches[0];
        if (event.touches.length !== 1 || Math.hypot(touch.clientX - mediaLongPressStart.x, touch.clientY - mediaLongPressStart.y) > 10) this.cancelMediaLongPress();
      }, { capture: true, passive: true });
      document.addEventListener("touchend", event => {
        this.cancelMediaLongPress();
        if (mediaLongPressOpened) {
          this.touchBackClickUntil = Date.now() + 450;
          if (event.cancelable) event.preventDefault();
          mediaLongPressOpened = false;
        }
      }, { capture: true, passive: false });
      document.addEventListener("touchcancel", () => this.cancelMediaLongPress(), { capture: true, passive: true });
      document.addEventListener("scroll", () => this.cancelMediaLongPress(), { capture: true, passive: true });
    },

    cancelMediaLongPress() {
      if (mediaLongPressTimer !== null) window.clearTimeout(mediaLongPressTimer);
      mediaLongPressTimer = null;
      mediaLongPressStart = null;
    },

    openMediaContextMenu(element, x, y) {
      if (!this.compactViewport || !element?.dataset.mediaMenuFolder) return;
      this.cancelMediaLongPress();
      this.cancelTouchBack();
      this.videoFeedGesture = null;
      this.touchBackClickUntil = Date.now() + 800;
      mediaMenuAnchor = element;
      const rect = element.getBoundingClientRect();
      this.mediaContextMenu = { open: true, folderName: element.dataset.mediaMenuFolder,
        pairIndex: Number(element.dataset.mediaMenuPair) || null,
        x: Math.max(12, Math.min(x || rect.left + 16, window.innerWidth - 232)),
        y: Math.max(12, Math.min(y || rect.top + 16, window.innerHeight - 112)) };
      this.$nextTick(() => this.$refs.mediaContextDelete?.focus({ preventScroll: true }));
    },

    closeMediaContextMenu() {
      this.cancelMediaLongPress();
      this.mediaContextMenu.open = false;
      mediaMenuAnchor = null;
    },

    chooseMediaContextDelete() {
      const { folderName, pairIndex } = this.mediaContextMenu;
      const element = mediaMenuAnchor;
      this.closeMediaContextMenu();
      // A virtualized card may have disappeared or been reused while the menu was open.
      if (!element?.isConnected || element.dataset.mediaMenuFolder !== folderName
          || (Number(element.dataset.mediaMenuPair) || null) !== pairIndex) return;
      if (pairIndex) this.askDeletePair(folderName, pairIndex);
      else this.askTrashFolder(folderName);
      this.$nextTick(() => {
        const confirmation = element.querySelector(pairIndex ? ".mini-confirm-button" : ".detail-trash-confirm button, .card-trash-zone .mini-confirm-button, [data-testid='video-delete-confirm']");
        confirmation?.scrollIntoView({ block: "nearest", inline: "nearest" });
        confirmation?.focus({ preventScroll: true });
      });
    },

    closeGalleryLayers() {
      this.closeMediaContextMenu();
      this.closeVideoNote(true);
      this.closeViewer(true);
      this.closeDetail(true);
    },

    scrollViewTop() {
      if (this.currentView !== 'settings') {
        settingsGroupToken += 1;
        settingsGroupAnimation?.cancel();
        settingsGroupAnimation = null;
      }
      const fadeWithinView = this.navigationView === this.currentView && ['gallery', 'recent-updates'].includes(this.currentView);
      this.navigationView = this.currentView;
      const workspace = document.querySelector(".workspace");
      if (workspace) {
        workspace.scrollTop = 0;
      }
      this.resetGalleryWindowToTop();
      window.scrollTo({ top: 0, behavior: "instant" });
      document.documentElement.scrollTop = 0;
      document.body.scrollTop = 0;
      this.updateHeaderState(0);
      this.scheduleFixedLayoutMetrics();
      if (fadeWithinView) this.$nextTick(() => this.fadeContent(document.querySelector('.view-stack > section:not([inert])')));
    },

    resetGalleryWindowToTop() {
      const items = this.gallery.items || [];
      const nextEnd = Math.min(items.length, this.galleryWindowSize());
      const needsReset = (this.galleryRenderStart || 0) > 0 || (this.galleryRenderEnd || 0) > nextEnd;
      if (!needsReset) {
        return;
      }
      document.querySelectorAll(".gallery-item-card").forEach((card) => this.stopHoverPreviewByCard(card));
      this.galleryRenderStart = 0;
      this.galleryRenderEnd = nextEnd;
      this.galleryWindowShiftLock = false;
      if (this.galleryWindowShiftRaf) {
        window.cancelAnimationFrame(this.galleryWindowShiftRaf);
        this.galleryWindowShiftRaf = null;
      }
      this.pruneGalleryWindowCaches();
      this.$nextTick(() => {
        this.observeGalleryCards();
        this.observeGalleryLoadSentinel();
      });
    },

    updateDetailHeader(event) {
      const scrollTop = Number(event?.target?.scrollTop || 0);
      if (scrollTop > 160) this.detailHeaderCompact = true;
      else if (scrollTop < 100) this.detailHeaderCompact = false;
    },

    scrollDetailTop() {
      this.detailHeaderCompact = false;
      const detailShell = this.$refs?.detailShell || document.querySelector(".detail-shell");
      if (!detailShell) {
        return;
      }
      detailShell.scrollTop = 0;
      if (typeof detailShell.scrollTo === "function") {
        detailShell.scrollTo({ top: 0, behavior: "auto" });
      }
    },

    measureGalleryMinHeight() {
      const element = document.querySelector(".gallery-main");
      if (!element) {
        return 0;
      }
      return Math.max(0, Math.ceil(element.getBoundingClientRect().height || 0));
    },

    lockGalleryMinHeight() {
      const measured = this.measureGalleryMinHeight();
      if (measured > 0) {
        this.galleryMinHeight = measured;
      }
    },

    releaseGalleryMinHeight() {
      const token = this.galleryRenderToken;
      window.setTimeout(() => {
        if (token !== this.galleryRenderToken) return;
        if (this.galleryLoading || this.galleryBatchQueue.length) {
          this.releaseGalleryMinHeight();
          return;
        }
        this.galleryMinHeight = 0;
      }, 120);
    },

    galleryMainStyle() {
      return this.galleryMinHeight > 0 ? `min-height: ${this.galleryMinHeight}px;` : "";
    },

    async api(url, options = {}) {
      const response = await fetch(url, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        if (payload.maintenance) window.location.reload();
        const error = new Error(payload.detail || payload.message || "请求失败");
        error.randomFeedExpired = !!payload.random_feed_expired;
        this.notify("error", "请求失败", error.message);
        throw error;
      }
      return response.json();
    },

    setImmediateTaskFeedback(message, extras = {}) {
      this.pullStatus = {
        ...this.pullStatus,
        ...extras,
        message,
      };
    },

    setImmediateSiteTaskFeedback(message, extras = {}) {
      this.siteStatus = {
        ...this.siteStatus,
        ...extras,
        message,
      };
    },

    notify(tone, title, message) {
      if (this.toastTimer) {
        window.clearTimeout(this.toastTimer);
      }
      this.toast = { open: true, tone, title, message };
      this.toastTimer = window.setTimeout(() => {
        this.toast.open = false;
      }, 2600);
    },

    fadeLoadedImage(event) {
      const image = event?.target;
      if (!image || !image.classList) {
        return;
      }
      image.classList.remove("image-loaded");
      void image.offsetWidth;
      image.classList.add("image-loaded");
    },

    fadeDetailPreviewImage(event) {
      const image = event?.target;
      if (!image || !image.classList || image.classList.contains("image-loaded")) {
        return;
      }
      image.classList.add("image-loaded");
    },

    closeToast() {
      if (this.toastTimer) {
        window.clearTimeout(this.toastTimer);
      }
      this.toast.open = false;
    },

    handleSpaceBack(event) {
      if (!(event.code === "Space" || event.key === " " || event.key === "Spacebar")
          || event.defaultPrevented || event.isComposing || event.keyCode === 229
          || event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return false;
      const target = event.target;
      if (target?.isContentEditable || target?.closest?.('input, textarea, select, iframe, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="searchbox"], [role="combobox"], [role="slider"]')) return false;
      event.preventDefault();
      // A held key must not dismiss several layers or trigger a focused button on keyup.
      if (!event.repeat) this.navigateTouchBack({ keyboard: true });
      return true;
    },

    handleKeydown(event) {
      if (this.handleSpaceBack(event)) return;
      this.trapModalTab(event);
      if (event.key === "Escape") {
        this.handleEscape();
        return;
      }
      if (this.videoNote.open && this.videoFeed.active && !event.target?.closest?.('input, textarea, select')) {
        if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
          event.preventDefault();
          this.stepVideoFeed(event.key === 'ArrowDown' ? 1 : -1);
          return;
        }
      }
      if (!this.viewer.open) {
        return;
      }
      if (event.target?.closest?.('input, textarea, select, [contenteditable="true"]')) return;
      if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) event.preventDefault();
      if (event.key === "ArrowUp") this.showPreviousPair();
      if (event.key === "ArrowDown") this.showNextPair();
      if (event.key === "ArrowRight" && !event.repeat) this.closeViewer();
      if (event.key === "ArrowLeft") this.revealViewerToolbar();
    },

    handleEscape() {
      if (this.currentFullscreenElement()) {
        this.exitCurrentFullscreen();
        return;
      }
      if (this.mediaContextMenu.open) {
        this.closeMediaContextMenu();
        return;
      }
      if (this.viewer.open) {
        this.closeViewer();
        return;
      }
      if (this.sourcePreview.open) {
        this.closeSourcePreview();
        return;
      }
      if (this.videoNote.open) {
        this.closeVideoNote();
        return;
      }
      if (this.resetSubscriptionIconsConfirmStep || this.resetSiteIconsConfirmStep || this.resetXhsIconsConfirmStep) {
        this.resetSubscriptionIconsConfirmStep = 0;
        this.resetSiteIconsConfirmStep = 0;
        this.resetXhsIconsConfirmStep = 0;
        return;
      }
      if (this.duplicateCleanupConfirmSignature) {
        this.duplicateCleanupConfirmSignature = null;
        return;
      }
      if (this.duplicateIgnoreConfirmSignature) {
        this.duplicateIgnoreConfirmSignature = null;
        return;
      }
      if (this.taskInspector.open) {
        this.closeTaskInspector();
        return;
      }
      if (this.detail.open) {
        this.closeDetail();
        return;
      }
      if (this.sidebarDrawerOpen) {
        this.closeSidebarDrawer();
        return;
      }
      this.closeToast();
    },

    currentSectionLabel() {
      if (this.currentView === "recent-updates") return "Smart Album";
      if (this.currentView === "duplicates") return "Duplicates";
      if (this.currentView === "review") return "Review";
      if (this.currentView === "logs") return "Filter Log";
      if (this.currentView === "tasks") return "Task Queue";
      if (this.currentView === "trash") return "Trash";
      if (this.currentView === "settings") return "Settings";
      if (this.currentView === "subscriptions") return "Subscriptions";
      const subscription = this.activeSelectedSubscription();
      if (subscription) return subscription.is_site ? "Site Subscription" : "UP Subscription";
      return "Library";
    },

    currentSectionTitle() {
      if (this.currentView === "recent-updates") return "最近更新";
      if (this.currentView === "duplicates") return "重复内容";
      if (this.currentView === "review") return "待审核动态";
      if (this.currentView === "logs") return "过滤日志";
      if (this.currentView === "tasks") return "任务队列";
      if (this.currentView === "trash") return "内容垃圾桶";
      if (this.currentView === "settings") return "设置";
      if (this.currentView === "subscriptions") return "订阅管理";
      const subscription = this.activeSelectedSubscription();
      if (subscription) return subscription.uname || subscription.name || subscription.uid;
      if (this.searchActive()) return this.searchQuery ? `搜索：${this.searchQuery}` : "搜索";
      if (this.currentView === "gallery" && this.sourceKind === "up") return "UP 订阅";
      if (this.currentView === "gallery" && this.sourceKind === "site") return "站点订阅";
      if (this.currentView === "gallery" && this.sourceKind === "xhs") return "小红书";
      return this.activeCategory().label;
    },

    currentSectionHeadline() {
      if (this.currentView === "recent-updates") return "按 B 站拉取、站点同步与小红书同步回看刚刚加入图库的动态";
      if (this.currentView === "duplicates") return "把相似图片贴文放在一起集中处理";
      if (this.currentView === "review") return "把推广动态拦在相簿之前";
      if (this.currentView === "logs") return "知道每一条动态为什么被筛出去";
      if (this.currentView === "settings") return "同步、账号权限与过滤策略";
      if (this.currentView === "subscriptions") return "统一管理 UP 主、站点与小红书订阅";
      const subscription = this.activeSelectedSubscription();
      if (subscription) return `只浏览 ${subscription.uname || subscription.name || subscription.uid} 的内容`;
      if (this.searchActive()) return "搜索标题、正文、订阅源和文件夹名";
      return `${this.activeCategory().label}，按更像系统相册的方式浏览`;
    },

    activeCategory() {
      return this.navItems.find((item) => item.key === this.category) || this.navItems[0];
    },

    activeSelectedSubscription() {
      if (this.currentView !== "gallery" || this.selectedSubscriptionUids.length !== 1) {
        return null;
      }
      const uid = String(this.selectedSubscriptionUids[0]);
      return (this.subscriptions || []).find((item) => String(item.uid) === uid) || {
        uid,
        uname: uid,
        is_site: uid.startsWith("site:"),
      };
    },

    showGallerySourceKindSwitch() {
      // 搜索时同样展示来源页签：搜索请求会带上当前 sourceKind（动态/站点/小红书），
      // 页签一旦隐藏就无法切换，用户会遇到“结果被悄悄过滤掉”却无从排查的情况。
      if (this.currentView === "recent-updates") {
        // 最近更新的请求同样携带当前 sourceKind（按任务类型过滤 B站拉取/站点同步/小红书同步），
        // 与全部项目、搜索保持一致：页签必须常驻，可随手切换来源。
        return true;
      }
      return this.currentView === "gallery" && this.selectedSubscriptionUids.length === 0;
    },

    compactStats() {
      const counts = this.meta.counts || {};
      return [
        {
          label: "动态总数",
          value: counts.all || 0,
        },
        {
          label: "Live Photo",
          value: counts.livephoto || 0,
        },
        {
          label: "收藏",
          value: counts.favorites || 0,
        },
        {
          label: "待审核",
          value: this.sidebarCounts.review ?? (this.reviewItems.length || 0),
        },
      ];
    },

    searchSignature() {
      return JSON.stringify([this.searchQuery, this.sourceKind, [...this.selectedSubscriptionUids].sort(),
        this.galleryViewMode, this.sortOrder, this.timeFilterApplied.startMonth, this.timeFilterApplied.endMonth]);
    },

    searchResultText() {
      if (this.galleryLoading || this.searchResultSignature !== this.searchSignature()) return "正在搜索…";
      const count = this.searchResultCount ?? 0;
      const unit = this.galleryViewMode === "pair" ? "组图片" : "条动态";
      const limits = [];
      const sourceLimited = this.sourceKind !== "all" && !this.selectedSubscriptionUids.length;
      if (sourceLimited) {
        const option = this.sourceKindOptions.find((item) => item.key === this.sourceKind);
        limits.push(`来源限定「${option?.label || this.sourceKind}」`);
      }
      if (this.timeFilterActive()) {
        limits.push(`时间限定「${this.timeFilterSummary()}」`);
      }
      if (!count && limits.length) {
        const tips = [];
        if (sourceLimited) tips.push("切换到「所有项目」");
        if (this.timeFilterActive()) tips.push("清空时间范围");
        return `找到 0 ${unit} · ${limits.join(" · ")}，试试${tips.join("或")}`;
      }
      if (limits.length) {
        return `找到 ${count} ${unit} · ${limits.join(" · ")}`;
      }
      return `找到 ${count} ${unit}`;
    },

    displayDate(value, dateOnly = false) {
      const date = this.parseDateValue(value);
      if (!date) return "暂无记录";
      const options = { year: "numeric", month: "2-digit", day: "2-digit" };
      if (!dateOnly && !/00:00:00(?:[+Z]|$)/.test(String(value))) {
        options.hour = "2-digit";
        options.minute = "2-digit";
      }
      return new Intl.DateTimeFormat("zh-CN", options).format(date);
    },

    futureDate(value) {
      const date = this.parseDateValue(value);
      return !!date && date.getTime() > Date.now() + 86400000;
    },

    taskStatusText(value) {
      return ({success: "已完成", partial: "部分完成", failed: "失败", running: "未结束记录", cancelled: "已取消", paused: "已暂停", queued: "等待中"})[value] || value;
    },

    taskDuration(item) {
      const start = this.parseDateValue(item.created_at);
      const end = this.parseDateValue(item.finished_at);
      if (!start || !end) return "";
      const seconds = Math.max(0, Math.round((end - start) / 1000));
      return seconds < 60 ? `耗时 ${seconds} 秒` : `耗时 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
    },

    viewerPositionText() {
      const sequence = this.resolvedViewerSequence();
      return `${this.currentViewerSequenceIndex(sequence) + 1} / ${sequence.length}`;
    },

    settingsFingerprint() {
      const {auth, intranet_config_error, ...values} = this.settings || {};
      return JSON.stringify([values, this.keywordText]);
    },

    settingsDirty() {
      return !!this.settingsBaseline && this.settingsFingerprint() !== this.settingsBaseline;
    },

    syncModalAccessibility() {
      const layer = this.topOverlayLayer(false);
      const name = layer === "fullscreen"
        ? this.currentFullscreenElement()?.closest?.("[data-modal-layer]")?.dataset.modalLayer || ""
        : layer;
      const active = name ? document.querySelector(`[data-modal-layer="${name}"]`) : null;
      const stack = this.modalFocusStack;
      let restore = null;
      if (stack.length && stack[stack.length - 1].element !== active) {
        const index = stack.findIndex((entry) => entry.element === active);
        if (index >= 0 || !active) {
          while (stack.length > Math.max(0, index + 1)) restore = stack.pop().opener;
        } else {
          stack.push({element: active, opener: document.activeElement});
        }
      } else if (active && !stack.length) {
        stack.push({element: active, opener: document.activeElement});
      }
      document.querySelectorAll("[data-modal-layer]").forEach((element) => {
        element.inert = element !== active;
        element.setAttribute("aria-modal", element === active ? "true" : "false");
      });
      const shell = document.querySelector(".shell");
      // Dialogs live inside .shell; making their ancestor inert disables the active dialog too.
      if (shell) {
        shell.inert = false;
        const main = shell.querySelector(":scope > main");
        if (main) main.inert = !!active && !main.contains(active);
        const sidebar = shell.querySelector(":scope > .sidebar");
        if (sidebar) sidebar.inert = !!active || (this.compactViewport && !this.sidebarDrawerOpen);
      }
      if (restore?.isConnected && !restore.closest("[inert]")) restore.focus({preventScroll: true});
      else if (restore && !active) document.querySelector('.sidebar-link.active')?.focus({preventScroll: true});
      if (active && !active.contains(document.activeElement)) {
        (name === 'viewer' ? active : active.querySelector('[data-modal-close]') || active).focus({preventScroll: true});
      }
    },

    trapModalTab(event) {
      if (event.key !== "Tab") return;
      const active = this.modalFocusStack[this.modalFocusStack.length - 1]?.element;
      if (!active) return;
      const elements = [...active.querySelectorAll('button, a[href], input, select, textarea, [tabindex="0"]')]
        .filter((element) => !element.disabled && !element.closest("[inert]") && element.getClientRects().length
          && window.getComputedStyle(element).visibility !== 'hidden');
      const first = elements[0] || active;
      const last = elements[elements.length - 1] || active;
      if (!elements.includes(document.activeElement) || (event.shiftKey ? document.activeElement === first : document.activeElement === last)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
    },

    searchActive() {
      return this.currentView === "gallery" && this.category === "search";
    },

    applySearch(query = this.searchDraft) {
      this.searchDraft = String(query || "").trim();
      this.searchQuery = this.searchDraft;
      this.searchResultCount = null;
      this.galleryRequestId += 1;
      this.category = "search";
      this.selectedSubscriptionUids = [];
      this.closeGalleryLayers();
      this.scrollViewTop();
      if (!this.searchQuery) {
        localStorage.removeItem("gallery_search_query");
        this.gallery = {
          ...this.gallery,
          items: [],
          total: 0,
          page: 1,
          total_pages: 1,
          view_mode: this.galleryViewMode,
        };
        this.gallerySkeletonCount = 0;
        this.displayedGalleryViewMode = this.galleryViewMode;
        return;
      }
      localStorage.setItem("gallery_search_query", this.searchQuery);
      this.refreshGalleryImmediately(true);
    },

    clearSearch() {
      this.searchDraft = "";
      this.searchQuery = "";
      this.searchResultCount = null;
      this.galleryRequestId += 1;
      this.galleryRenderToken += 1;
      this.galleryLoading = false;
      this.cancelGalleryBatchWork();
      localStorage.removeItem("gallery_search_query");
      this.category = "search";
      this.gallery = {
        ...this.gallery,
        items: [],
        total: 0,
        page: 1,
        total_pages: 1,
        view_mode: this.galleryViewMode,
      };
      this.gallerySkeletonCount = 0;
      this.displayedGalleryViewMode = this.galleryViewMode;
    },

    sidebarCount(key) {
      if (key === "search") {
        return this.searchActive() && this.searchQuery && this.searchResultSignature === this.searchSignature() ? (this.searchResultCount ?? "") : "";
      }
      if (key in (this.sidebarCounts || {})) {
        return this.sidebarCounts[key] || 0;
      }
      return this.meta.counts?.[key] || 0;
    },

    applySidebarCounts(payload) {
      const counts = payload?.counts || {};
      this.sidebarCounts = { ...this.sidebarCounts, ...counts };
      this.sidebarCountUpdatedAt = { ...this.sidebarCountUpdatedAt, ...(payload?.updated_at || {}) };
      this.meta = {
        ...this.meta,
        counts: {
          ...(this.meta.counts || {}),
          all: counts.all ?? this.meta.counts?.all ?? 0,
          favorites: counts.favorites ?? this.meta.counts?.favorites ?? 0,
          livephoto: counts.livephoto ?? this.meta.counts?.livephoto ?? 0,
          video: counts.video ?? this.meta.counts?.video ?? 0,
        },
      };
      if (counts.sites !== undefined) {
        this.siteStats = { ...this.siteStats, source_count: counts.sites };
      }
    },

    async loadSidebarCounts() {
      const payload = await this.api("/api/sidebar-counts");
      this.applySidebarCounts(payload);
    },

    async loadBootstrap() {
      await Promise.allSettled([
        this.api("/api/settings").then(settings => {
          // The settings page may already have loaded and accepted edits while
          // another bootstrap request is still pending.
          if (this.lazyLoaded.settings || this.settingsDirty()) return;
          this.settings = settings || {};
          this.savedGalleryDefaultRandom = settings?.gallery_default_random === true;
          this.settingsError = settings?.intranet_config_error || "";
        }),
        this.refreshStatus(),
        this.refreshMeta(),
        this.loadSubscriptions(),
      ]);
    },

    scheduleStatusRefresh() {
      if (this.statusRefreshTimer) {
        window.clearTimeout(this.statusRefreshTimer);
      }
      const running = !!this.pullStatus?.running || !!this.siteStatus?.running;
      const delay = running ? 3000 : 20000;
      this.statusRefreshTimer = window.setTimeout(async () => {
        this.statusRefreshTimer = null;
        try {
          await this.refreshStatus();
        } finally {
          this.scheduleStatusRefresh();
        }
      }, delay);
    },

    async refreshSidebarCounts(keys = null) {
      const payload = await this.api("/api/sidebar-counts/refresh", {
        method: "POST",
        body: JSON.stringify({ keys }),
      });
      this.applySidebarCounts(payload);
    },

    applyMutationSidebarCounts(result, fallbackKeys = null) {
      if (result?.sidebar_counts) {
        this.applySidebarCounts(result.sidebar_counts);
        return Promise.resolve();
      }
      return this.refreshSidebarCounts(fallbackKeys);
    },

    schedulePostMutationRefresh(result, fallbackKeys = null, extras = {}) {
      if (result?.sidebar_counts) {
        this.applySidebarCounts(result.sidebar_counts);
      }
      this.scheduleIdleWork(() => {
        const tasks = [
          this.refreshMeta().catch(() => {}),
          result?.sidebar_counts ? Promise.resolve() : this.refreshSidebarCounts(fallbackKeys).catch(() => {}),
        ];
        if (extras.trash || this.currentView === "trash" || this.lazyLoaded.trash) {
          tasks.push(this.refreshTrash().catch(() => {}));
        }
        if (extras.tasks || this.currentView === "tasks" || this.lazyLoaded.tasks) {
          tasks.push(this.refreshTasks().catch(() => {}));
        }
        if (extras.recentUpdates || this.currentView === "recent-updates" || this.lazyLoaded.recentUpdates) {
          tasks.push(this.refreshRecentUpdates().catch(() => {}));
        }
        if (this.currentView === "duplicates" || this.lazyLoaded.duplicates) {
          tasks.push(this.refreshDuplicates().catch(() => {}));
        }
        Promise.all(tasks).catch(() => {});
      }, 1200);
    },

    installFixedLayoutObservers() {
      if (this.layoutResizeObserver) {
        this.layoutResizeObserver.disconnect();
        this.layoutResizeObserver = null;
      }
      const targets = [this.$refs.shell, this.$refs.sidebar, this.$refs.workspace, this.$refs.topbar, this.$refs.galleryStage].filter(Boolean);
      if (typeof ResizeObserver === "function") {
        this.layoutResizeObserver = new ResizeObserver(() => {
          const width = this.$refs.galleryStage?.clientWidth || 0;
          if (width > 0 && width !== this.galleryStageWidth) this.galleryStageWidth = width;
          this.scheduleFixedLayoutMetrics();
          this.scheduleRecentUpdatesMeasure();
        });
        targets.forEach((target) => this.layoutResizeObserver.observe(target));
      }
      this.scheduleFixedLayoutMetrics();
    },

    scheduleFixedLayoutMetrics() {
      if (this.layoutMetricRaf) {
        return;
      }
      this.layoutMetricRaf = window.requestAnimationFrame(() => {
        this.layoutMetricRaf = null;
        this.updateFixedLayoutMetrics();
      });
    },

    setRootStyleVar(name, value) {
      const root = document.documentElement;
      if (root.style.getPropertyValue(name) !== value) {
        root.style.setProperty(name, value);
      }
    },

    updateFixedLayoutMetrics() {
      const root = document.documentElement;
      const header = this.$refs.topbar;
      if (header) {
        const top = Number.parseFloat(getComputedStyle(header).top) || 0;
        this.setRootStyleVar('--page-toolbar-top', `${Math.ceil(header.getBoundingClientRect().height + top + 6)}px`);
      }
      const pinned = window.matchMedia("(min-width: 1101px) and (min-aspect-ratio: 11/10)").matches;
      if (!pinned) {
        root.classList.remove("layout-pinned");
        this.layoutTopbarReserve = 0;
        [
          "--layout-sidebar-left",
          "--layout-sidebar-top",
          "--layout-sidebar-width",
          "--layout-sidebar-height",
          "--layout-topbar-left",
          "--layout-topbar-top",
          "--layout-topbar-width",
          "--layout-topbar-reserve",
        ].forEach((name) => root.style.removeProperty(name));
        return;
      }

      const shell = this.$refs.shell || document.querySelector(".shell");
      const workspace = this.$refs.workspace || document.querySelector(".workspace");
      const topbar = this.$refs.topbar || document.querySelector(".topbar");
      if (!shell || !workspace || !topbar) {
        return;
      }

      const lengthValue = (value, fallback = 0) => {
        const parsed = Number.parseFloat(value);
        return Number.isFinite(parsed) ? parsed : fallback;
      };
      const rootStyle = getComputedStyle(root);
      const shellStyle = getComputedStyle(shell);
      const shellRect = shell.getBoundingClientRect();
      const sidebarFallback = lengthValue(
        rootStyle.getPropertyValue(this.sidebarCollapsed ? "--sidebar-collapsed" : "--sidebar-width"),
        this.sidebarCollapsed ? 72 : 236,
      );
      const sidebarWidth = lengthValue((shellStyle.gridTemplateColumns || "").split(" ")[0], sidebarFallback);
      const insetTop = Math.round(Math.max(12, lengthValue(shellStyle.paddingTop, 20)));
      const insetLeft = Math.round(shellRect.left + lengthValue(shellStyle.paddingLeft, 20));
      const insetRight = Math.round(Math.max(12, lengthValue(shellStyle.paddingRight, 20)));
      const columnGap = lengthValue(shellStyle.columnGap, 20);
      const sidebarHeight = Math.max(240, Math.round(window.innerHeight - insetTop * 2));
      const topbarLeft = Math.round(insetLeft + sidebarWidth + columnGap);
      const topbarWidth = Math.max(320, Math.round(window.innerWidth - topbarLeft - insetRight));

      this.setRootStyleVar("--layout-sidebar-left", `${insetLeft}px`);
      this.setRootStyleVar("--layout-sidebar-top", `${insetTop}px`);
      this.setRootStyleVar("--layout-sidebar-width", `${Math.round(sidebarWidth)}px`);
      this.setRootStyleVar("--layout-sidebar-height", `${sidebarHeight}px`);
      this.setRootStyleVar("--layout-topbar-left", `${topbarLeft}px`);
      this.setRootStyleVar("--layout-topbar-top", `${insetTop}px`);
      this.setRootStyleVar("--layout-topbar-width", `${topbarWidth}px`);
      root.classList.add("layout-pinned");

      const topbarHeight = Math.ceil(topbar.getBoundingClientRect().height || 0);
      const measuredReserve = Math.max(44, topbarHeight + 10);
      const reserve = this.headerCompact && this.layoutTopbarReserve
        ? Math.max(this.layoutTopbarReserve, measuredReserve)
        : measuredReserve;
      this.layoutTopbarReserve = reserve;
      this.setRootStyleVar("--layout-topbar-reserve", `${reserve}px`);
    },

    updateHeaderState(scrollTop = window.scrollY || window.pageYOffset || 0) {
      const previous = this.headerCompact;
      const compactEnter = 112;
      const compactExit = 18;
      if (this.headerCompact) {
        this.headerCompact = scrollTop > compactExit;
      } else {
        this.headerCompact = scrollTop > compactEnter;
      }
      if (previous !== this.headerCompact) {
        this.scheduleFixedLayoutMetrics();
      }
    },

    resolveGalleryColumns() {
      const width = this.galleryStageWidth || this.$refs?.galleryStage?.clientWidth || window.innerWidth || 1280;
      if (this.compactViewport || this.narrowViewport) {
        return Math.max(2, Math.min(4, Math.floor((width + 8) / 184)));
      }
      const target = Math.max(1, Number(this.galleryColumnCount) || 1);
      if (this.galleryColumnCache && Math.abs(width - this.galleryColumnCacheWidth) < 24) {
        return Math.min(target, this.galleryColumnCache);
      }
      const minItemWidth = this.galleryViewMode === "pair" ? 150 : 220;
      const gap = 18;
      const fit = Math.max(1, Math.floor((width + gap) / (minItemWidth + gap)));
      this.galleryColumnCache = fit;
      this.galleryColumnCacheWidth = width;
      return Math.min(target, fit);
    },

    galleryMasonryStyle() {
      return `--gallery-columns: ${this.resolveGalleryColumns()};`;
    },

    resolveDetailColumns() {
      const target = Math.max(1, Number(this.detailColumnCount) || 1);
      const width = this.$refs.detailGrid?.clientWidth || this.$refs.detailShell?.clientWidth || window.innerWidth || 1280;
      if (this.detailColumnCache && Math.abs(width - this.detailColumnCacheWidth) < 24) {
        return Math.min(target, this.detailColumnCache);
      }
      const minItemWidth = 132;
      const gap = 16;
      const fit = Math.max(1, Math.floor((width + gap) / (minItemWidth + gap)));
      this.detailColumnCache = fit;
      this.detailColumnCacheWidth = width;
      return Math.min(target, fit);
    },

    detailGridStyle() {
      return `--detail-columns: ${this.resolveDetailColumns()};`;
    },

    detailWindowSize() {
      const columns = Math.max(1, this.resolveDetailColumns());
      const viewportHeight = this.$refs?.detailShell?.clientHeight || 0;
      const visibleRows = Math.ceil(viewportHeight / Math.max(1, this.detailEstimatedRowHeight));
      return columns * Math.max(this.detailWindowRowCount, visibleRows + 6);
    },

    detailWindowItems() {
      const pairs = this.detail.pairs || [];
      const size = this.detailWindowSize();
      const start = Math.max(0, Math.min(this.detailRenderStart || 0, Math.max(0, pairs.length - 1)));
      const end = Math.min(pairs.length, Math.max(start, this.detailRenderEnd || start + size));
      return pairs.slice(start, end);
    },

    detailVirtualSpacerStyle(position) {
      const pairs = this.detail.pairs || [];
      const columns = Math.max(1, this.resolveDetailColumns());
      const start = Math.max(0, this.detailRenderStart || 0);
      const end = Math.min(pairs.length, Math.max(start, this.detailRenderEnd || start + this.detailWindowSize()));
      const rowsBefore = Math.floor(start / columns);
      const rowsAfter = Math.ceil(pairs.length / columns) - Math.ceil(end / columns);
      const rows = position === "top" ? rowsBefore : rowsAfter;
      const rowHeight = Math.max(1, this.detailEstimatedRowHeight || 270);
      const height = rows > 0 ? Math.max(0, rows * rowHeight - Math.max(0, this.detailGridGap || 0)) : 0;
      return height > 0 ? `height: ${height}px;` : "display: none;";
    },

    resetDetailWindow() {
      this.detailRenderStart = 0;
      this.detailRenderEnd = Math.min((this.detail.pairs || []).length, this.detailWindowSize());
      this.detailEstimatedRowHeight = 270;
      this.detailGridGap = 14;
      this.detailWindowShiftLock = false;
      if (this.detailWindowShiftRaf) {
        window.cancelAnimationFrame(this.detailWindowShiftRaf);
        this.detailWindowShiftRaf = null;
      }
      this.$nextTick(() => {
        this.measureDetailWindow();
        if (!detailResizeObserver && typeof ResizeObserver === "function") {
          detailResizeObserver = new ResizeObserver(() => this.scheduleDetailWindowShift());
          [this.$refs?.detailGrid, this.$refs?.detailShell].filter(Boolean)
            .forEach(element => detailResizeObserver.observe(element));
        }
        this.scheduleDetailWindowShift();
        this.refreshDetailTextOverflow();
      });
    },

    measureDetailWindow() {
      const grid = this.$refs?.detailGrid || document.querySelector(".detail-grid");
      const card = grid?.querySelector(".detail-card[data-detail-key]");
      if (!card) {
        return;
      }
      const styles = window.getComputedStyle(grid);
      const gap = Number.parseFloat(styles.rowGap || styles.gap || 0) || 0;
      // Detail cards are square. Use the actual fractional track width, unaffected by
      // drawer/hover transforms, instead of clamping small cards to a 180px estimate.
      const rowHeight = Number.parseFloat(styles.gridTemplateColumns) + gap;
      if (Number.isFinite(rowHeight) && rowHeight > gap) {
        this.detailGridGap = gap;
        this.detailEstimatedRowHeight = rowHeight;
      }
    },

    stopDetailHoverPreviews() {
      document.querySelectorAll(".detail-card").forEach((card) => this.stopHoverPreviewByCard(card));
    },

    scheduleDetailWindowShift() {
      if (!this.detail.open) {
        return;
      }
      if (this.detailWindowShiftRaf || this.detailWindowShiftLock) {
        return;
      }
      this.detailWindowShiftRaf = window.requestAnimationFrame(() => {
        this.detailWindowShiftRaf = null;
        this.measureDetailWindow();
        this.shiftDetailWindowIfNeeded();
      });
    },

    shiftDetailWindowIfNeeded() {
      if (this.detailWindowShiftLock || !this.detail.open) {
        return;
      }
      const shell = this.$refs?.detailShell || document.querySelector(".detail-shell");
      const cards = Array.from(document.querySelectorAll(".detail-card[data-detail-key]"));
      if (!shell || !cards.length) {
        return;
      }
      const pairs = this.detail.pairs || [];
      const columns = Math.max(1, this.resolveDetailColumns());
      const size = this.detailWindowSize();
      const start = Math.max(0, this.detailRenderStart || 0);
      const end = Math.min(pairs.length, Math.max(start, this.detailRenderEnd || start + size));
      const shellRect = shell.getBoundingClientRect();
      const grid = this.$refs?.detailGrid || document.querySelector(".detail-grid");
      if (!grid) return;
      const pitch = Math.max(1, this.detailEstimatedRowHeight);
      const offset = Math.max(0, shellRect.top + shell.clientTop - grid.getBoundingClientRect().top);
      const firstVisibleRow = Math.floor(offset / pitch);
      const lastVisibleRow = Math.ceil((offset + shell.clientHeight) / pitch);
      const totalRows = Math.ceil(pairs.length / columns);
      const windowRows = size / columns;
      const nextRow = Math.min(Math.max(0, firstVisibleRow - 3), Math.max(0, totalRows - windowRows));
      const nextStart = nextRow * columns;
      const nextEnd = Math.min(pairs.length, nextStart + size);
      const nearEdge = firstVisibleRow < start / columns + 2 || lastVisibleRow > Math.ceil(end / columns) - 2;
      if ((!nearEdge && end - start === size && start % columns === 0) || (nextStart === start && nextEnd === end)) {
        return;
      }
      this.stopDetailHoverPreviews();
      this.detailWindowShiftLock = true;
      this.detailRenderStart = nextStart;
      this.detailRenderEnd = nextEnd;
      // Exact row spacers keep the total height and retained card positions stable.
      // Do not write scrollTop here: it interrupts native touch/trackpad momentum.
      this.$nextTick(() => {
        this.detailWindowShiftLock = false;
      });
    },

    tinyThumbBackground(url) {
      const value = String(url || "").trim();
      if (!value) {
        return "";
      }
      return `background-image: url("${value.replace(/["\\\n\r]/g, "\\$&")}");`;
    },

    galleryQualityFor(item) {
      const key = this.galleryItemKey(item);
      return key && this.galleryCardQuality[key] === "small" ? "small" : "pending";
    },

    galleryQualityRank(quality) {
      return { pending: 0, small: 1, thumb: 2 }[quality] ?? 0;
    },

    promoteGalleryQuality(key, quality) {
      if (!key) {
        return;
      }
      const current = this.galleryQualityUpdateQueue[key] || this.galleryCardQuality[key] || "pending";
      if (this.galleryQualityRank(current) >= this.galleryQualityRank(quality)) {
        return;
      }
      this.galleryQualityUpdateQueue[key] = quality;
      if (this.galleryQualityUpdateRaf) {
        return;
      }
      // 仅做同帧合批、下一帧立即落地：此前附加的 120ms 防抖计时器会在首次加载/
      // 快速翻页时让质量类迟迟不更新，卡片灰框久留、图片随后集中突现。
      this.galleryQualityUpdateRaf = window.requestAnimationFrame(() => this.flushGalleryQualityUpdates());
    },

    flushGalleryQualityUpdates() {
      if (this.galleryQualityUpdateRaf) {
        window.cancelAnimationFrame(this.galleryQualityUpdateRaf);
        this.galleryQualityUpdateRaf = null;
      }
      const updates = this.galleryQualityUpdateQueue;
      this.galleryQualityUpdateQueue = {};
      if (Object.keys(updates).length) {
        this.galleryCardQuality = { ...this.galleryCardQuality, ...updates };
      }
    },

    galleryTileUrl(tile) {
      return pickThumbUrl([tile]);
    },

    galleryPairImageUrl(item) {
      return pickThumbUrl([item, item?.image, item?.livephoto]);
    },

    gallerySmallUrlsForItem(item) {
      const urls = new Set();
      const tiles = this.galleryPreviewTiles(item);
      if (tiles.length) {
        for (const tile of tiles.slice(0, 4)) {
          const url = this.galleryTileUrl(tile);
          if (url) {
            urls.add(url);
          }
        }
      } else {
        const url = this.galleryPairImageUrl(item);
        if (url) {
          urls.add(url);
        }
      }
      return Array.from(urls);
    },

    queueGallerySmallItem(item) {
      const key = this.galleryItemKey(item);
      if (!key || this.galleryCardQuality[key] === "small") return false;
      this.promoteGalleryQuality(key, "small");
      return true;
    },

    assetVisualStyle(item) {
      return `aspect-ratio: ${item.display_ratio || "1 / 1"}`;
    },

    galleryDensityCaption() {
      const count = this.resolveGalleryColumns();
      if (this.compactViewport || this.narrowViewport) return `按内容宽度自动适配，每行 ${count} 张（2～4 张）`;
      return `期望 ${this.galleryColumnCount} 列，当前窗口 ${count} 列`;
    },

    creatorName(item) {
      if (!item) return "未知作者";
      if (item.creator_name) return String(item.creator_name);
      if (String(item.folder_name || "").startsWith("xhs_")) {
        return item.author_name || (item.author_id ? `作者 ${item.author_id}` : "未知作者");
      }
      return item.subscription_name || "默认订阅";
    },

    creatorSourceKind(item) {
      if (["up", "xhs", "site"].includes(item?.source_kind)) return item.source_kind;
      if (String(item?.folder_name || "").startsWith("xhs_")) return "xhs";
      const uid = String(item?.subscription_uid || "");
      const subscription = (this.subscriptions || []).find(entry => String(entry.uid) === uid);
      if (subscription?.source_type === "xhs_likes") return "xhs";
      return uid.startsWith("site:") ? "site" : "up";
    },

    creatorSubscriptionUid(item) {
      return String(item?.subscription_uid || (this.creatorSourceKind(item) === "up" ? this.settings.host_mid || "" : ""));
    },

    openCreatorSubscription(item) {
      const uid = this.creatorSubscriptionUid(item);
      if (uid) this.selectSubscription(uid, this.creatorSourceKind(item));
    },

    creatorProfileUrl(item) {
      const raw = item?.creator_url || (item?.author_id
        ? `https://www.xiaohongshu.com/user/profile/${encodeURIComponent(item.author_id)}`
        : /^\d+$/.test(String(item?.subscription_uid || "")) ? `https://space.bilibili.com/${item.subscription_uid}` : "");
      try {
        const url = new URL(raw);
        return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
      } catch (_) { return ""; }
    },

    detailContentText() {
      const folder = this.detail?.folder || {};
      if (this.isSiteGalleryItem(folder)) {
        return String(folder.title || folder.text_prefix || "动态内容").trim();
      }
      return String(folder.title || folder.text_prefix || "动态内容").trim();
    },

    detailTextExpandable() {
      return this.detailTextOverflowing || this.detailTextExpanded || this.detailContentText().length > 36;
    },

    refreshDetailTextOverflow() {
      if (!this.detail.open) {
        this.detailTextOverflowing = false;
        return;
      }
      const element = this.$refs?.detailContentText || document.querySelector(".detail-content-toggle > span");
      if (!element || this.detailTextExpanded) {
        return;
      }
      this.detailTextOverflowing = element.scrollWidth > element.clientWidth + 1;
    },

    toggleDetailText() {
      if (!this.detailTextExpandable()) {
        return;
      }
      this.detailTextExpanded = !this.detailTextExpanded;
      if (!this.detailTextExpanded) {
        this.$nextTick(() => this.refreshDetailTextOverflow());
      }
    },

    setGalleryColumnCount(value) {
      this.galleryColumnCount = Number(value);
      this.galleryColumnCache = null;
      this.setInteracting(220);
      this.scheduleIdleWork(() => localStorage.setItem("gallery_column_count", String(this.galleryColumnCount)), 500);
      this.$nextTick(() => {
        this.observeGalleryCards();
        this.scheduleGallerySmallPromotion(120);
      });
    },

    setDetailColumnCount(value) {
      this.detailColumnCount = Number(value);
      this.detailColumnCache = null;
      this.setInteracting(220);
      this.scheduleIdleWork(() => localStorage.setItem("detail_column_count", String(this.detailColumnCount)), 500);
      this.detailRenderEnd = Math.min(
        (this.detail.pairs || []).length,
        this.detailRenderStart + this.detailWindowSize(),
      );
      this.$nextTick(() => {
        this.measureDetailWindow();
        this.scheduleDetailWindowShift();
      });
    },

    setSortGroup(random) {
      this.setTimeSortMode(random ? "random" : this.orderedSortMode);
    },

    setTimeSortMode(mode) {
      if (!["desc", "asc", "random", "imported"].includes(mode)) {
        return;
      }
      if (this.timeSortMode === mode) return;
      if (mode !== "random") this.orderedSortMode = mode;
      this.timeSortMode = mode;
      localStorage.setItem("gallery_time_sort_mode", mode);
      const resolvedOrder = mode === "random" ? "random" : mode;
      if (this.sortOrder !== resolvedOrder) {
        this.sortOrder = resolvedOrder;
        localStorage.setItem("gallery_sort_order", resolvedOrder);
      }
      this.closeGalleryLayers();
      this.scrollViewTop();
      this.refreshGalleryImmediately(true);
    },

    async refreshMeta() {
      const previousCounts = this.meta.counts || {};
      const payload = await this.api("/api/gallery/meta");
      this.meta = {
        ...payload,
        counts: { ...previousCounts, ...(payload.counts || {}) },
      };
      this.sidebarCounts = { ...this.sidebarCounts, ...(payload.counts || {}) };
      if (!this.selectedSubscriptionUids.length) {
        this.subscriptions = (this.subscriptions || []).map((item) => {
          const metaItem = (this.meta.subscriptions || []).find((entry) => entry.uid === item.uid);
          return metaItem ? { ...item, folder_count: metaItem.count } : item;
        });
      }
    },

    async loadSubscriptions() {
      const payload = await this.api("/api/subscriptions");
      const previousExpanded = { ...(this.subscriptionExpanded || {}) };
      this.latestSubscriptionPullAt = payload.latest_pull_at || this.latestSubscriptionPullAt || "";
      this.subscriptions = (payload.items || []).map((item) => this.normalizeSubscriptionItem(item));
      this.sidebarCounts = { ...this.sidebarCounts, subscriptions: this.subscriptions.length };
      this.subscriptionExpanded = Object.fromEntries(
        this.subscriptions.map((item) => [String(item.uid), !!previousExpanded[String(item.uid)]]),
      );
    },

    normalizeSubscriptionItem(item) {
      return {
        ...item,
        is_site: !!item.is_site || String(item.uid || "").startsWith("site:"),
        avatar_url: item.avatar_url || "",
        avatar_tiny_url: item.avatar_tiny_url || "",
        icon_url: item.icon_url || item.avatar_url || "",
        icon_tiny_url: item.icon_tiny_url || item.avatar_tiny_url || "",
        pull_images: !!item.pull_images,
        pull_livephoto: !!item.pull_livephoto,
        include_forwarded: !!item.include_forwarded,
      };
    },

    upSubscriptions() {
      return (this.subscriptions || []).filter((item) => !item.is_site && !String(item.uid || "").startsWith("site:"));
    },

    siteSubscriptions() {
      return (this.subscriptions || []).filter((item) => (item.is_site || String(item.uid || "").startsWith("site:")) && item.source_type !== 'xhs_likes');
    },

    setSubscriptionPanel(panel) {
      if (!["overview", "up", "site", "xhs"].includes(panel) || this.subscriptionPanel === panel) {
        return;
      }
      this.subscriptionPanel = panel;
      this.xhsResetSteps = {};
      localStorage.setItem("subscription_panel", panel);
      this.scrollViewTop();
      if (panel === "xhs") {
        this.loadXhsSources();
      } else if (panel === "site") {
        this.refreshSites();
        this.refreshSidebarCounts(["sites", "subscriptions"]).catch(() => {});
      } else if (panel === "overview") {
        this.loadSubscriptions();
        this.refreshSites();
        this.refreshSidebarCounts(["sites", "subscriptions"]).catch(() => {});
      } else {
        this.loadSubscriptions();
        this.refreshSidebarCounts(["subscriptions"]).catch(() => {});
      }
    },

    async setSettingsGroup(group) {
      const token = ++settingsGroupToken;
      settingsGroupAnimation?.cancel();
      settingsGroupAnimation = null;
      if (this.settingsGroup === group) return;
      const grid = document.querySelector('.settings-grid');
      if (grid?.animate && !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
        settingsGroupAnimation = grid.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 90, fill: 'forwards' });
        await settingsGroupAnimation.finished.catch(() => {});
        if (token !== settingsGroupToken) return;
      }
      this.settingsGroup = group;
      this.scrollViewTop();
      await this.$nextTick();
      if (token !== settingsGroupToken) return;
      settingsGroupAnimation?.cancel();
      settingsGroupAnimation = null;
      this.fadeContent(grid, 0);
    },

    fadeContent(element, from = 0.35) {
      if (!element?.animate || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
      element.getAnimations().forEach(animation => animation.cancel());
      element.animate([{ opacity: from }, { opacity: 1 }], { duration: 180, easing: 'ease-out' });
    },

    subscriptionPanelIndex() {
      return ["overview", "up", "site", "xhs"].indexOf(this.subscriptionPanel);
    },

    subscriptionPanelIndicatorStyle() {
      const index = Math.max(0, this.subscriptionPanelIndex());
      return `width: calc(25% - 4px); transform: translateX(${index * 100}%);`;
    },

    isSitePanelActive() {
      return this.currentView === "subscriptions" && this.subscriptionPanel === "site";
    },

    setSubscriptionOverviewSort(value) {
      this.subscriptionOverviewSort = value;
      localStorage.setItem("subscription_overview_sort", value);
    },

    setSubscriptionOverviewGroup(value) {
      this.subscriptionOverviewGroup = value;
      localStorage.setItem("subscription_overview_group", value);
    },

    subscriptionOverviewItems() {
      const query = String(this.subscriptionSearch || "").trim().toLowerCase();
      const items = (this.subscriptions || []).filter((item) => {
        if (!query) return true;
        const haystack = [
          item.uid,
          item.uname,
          item.name,
          item.slug,
          item.status,
          item.is_site ? "站点" : "up",
        ].join(" ").toLowerCase();
        return haystack.includes(query);
      });
      const valueFor = (item, key) => {
        if (key === "recent") return this.parseDateValue(item.latest_content_at || item.latest_content_ts)?.getTime() || 0;
        if (key === "posts") return Number(item.folder_count || item.post_count || 0);
        if (key === "assets") return this.subscriptionImageTotal(item);
        return String(item.uname || item.name || item.uid || "").toLowerCase();
      };
      return [...items].sort((left, right) => {
        const mode = this.subscriptionOverviewSort;
        if (mode === "posts" || mode === "assets" || mode === "recent") {
          return valueFor(right, mode) - valueFor(left, mode);
        }
        return String(valueFor(left, "name")).localeCompare(String(valueFor(right, "name")), "zh-Hans-CN");
      });
    },

    subscriptionOverviewGroups() {
      const items = this.subscriptionOverviewItems();
      if (this.subscriptionOverviewGroup === "status") {
        return [
          { key: "active", title: "订阅中", items: items.filter((item) => item.status !== "paused") },
          { key: "paused", title: "已暂停", items: items.filter((item) => item.status === "paused") },
        ].filter((group) => group.items.length);
      }
      if (this.subscriptionOverviewGroup === "none") {
        return [{ key: "all", title: "全部订阅", items }];
      }
      return [
        { key: "up", title: "UP 主动态", items: items.filter((item) => !item.is_site) },
        { key: "site", title: "站点订阅", items: items.filter((item) => item.is_site && item.source_type !== 'xhs_likes') },
        { key: "xhs", title: "小红书", items: items.filter((item) => item.source_type === 'xhs_likes') },
      ].filter((group) => group.items.length);
    },

    subscriptionOverviewPostText(item) {
      const posts = Number(item.folder_count || item.post_count || 0);
      return `${posts}贴`;
    },

    subscriptionOverviewImageText(item) {
      return `${this.subscriptionImageTotal(item)}张`;
    },

    subscriptionOverviewDateText(item) {
      return `最新内容：${this.formatDateYMD(item?.latest_content_at || item?.latest_content_ts) || "暂无"}`;
    },

    subscriptionOverviewDateStyle(item) {
      return this.futureDate(item?.latest_content_at || item?.latest_content_ts) ? "color: #934112" : "color: var(--muted)";
    },

    subscriptionSyncText(item) {
      if (item.status === "paused" || item.enabled === false) return "已暂停";
      const value = item.latest_pull_at || item.last_sync_at;
      return value ? `最近同步：${this.displayDate(value)}` : "单源同步时间未记录";
    },

    subscriptionImageTotal(item) {
      const explicit = Number(item?.image_total);
      if (Number.isFinite(explicit)) {
        return Math.max(0, explicit);
      }
      const images = Number(item?.image_count);
      if (Number.isFinite(images)) {
        return Math.max(0, images);
      }
      const assets = Number(item?.asset_count);
      return Number.isFinite(assets) ? Math.max(0, assets) : 0;
    },

    openSubscriptionOverviewItem(item) {
      if (!item?.uid) {
        return;
      }
      this.selectSubscription(item.uid);
    },

    subscriptionIconUrl(item) {
      const key = String(item?.uid || "");
      if (!key || this.iconLoadFailures[key]) {
        return "";
      }
      return this.versionedIconUrl(
        item?.icon_url || item?.avatar_url || item?.site_icon_url || "",
        this.iconRefreshVersions[key] || item?.updated_at,
      );
    },

    subscriptionIconTinyUrl(item) {
      const key = String(item?.uid || "");
      if (!key || this.iconLoadFailures[key]) {
        return "";
      }
      return this.versionedIconUrl(
        item?.icon_tiny_url || item?.avatar_tiny_url || item?.site_icon_tiny_url || "",
        this.iconRefreshVersions[key] || item?.updated_at,
      );
    },

    subscriptionIconTinyStyle(item) {
      return this.tinyThumbBackground(this.subscriptionIconTinyUrl(item));
    },

    handleSubscriptionIconError(item) {
      const key = String(item?.uid || "");
      if (!key) {
        return;
      }
      this.iconLoadFailures = { ...this.iconLoadFailures, [key]: true };
    },

    siteSourceIconUrl(source) {
      const key = `site:${source?.id || ""}`;
      if (!source?.icon_url || this.iconLoadFailures[key]) {
        return "";
      }
      return this.versionedIconUrl(source.icon_url, this.iconRefreshVersions[key] || source.updated_at);
    },

    siteSourceIconTinyUrl(source) {
      const key = `site:${source?.id || ""}`;
      if (!source?.icon_tiny_url || this.iconLoadFailures[key]) {
        return "";
      }
      return this.versionedIconUrl(source.icon_tiny_url, this.iconRefreshVersions[key] || source.updated_at);
    },

    siteSourceIconTinyStyle(source) {
      return this.tinyThumbBackground(this.siteSourceIconTinyUrl(source));
    },

    versionedIconUrl(url, version) {
      const value = String(url || "").trim();
      if (!value || !value.startsWith("/storage/") || !version) {
        return value;
      }
      const separator = value.includes("?") ? "&" : "?";
      return `${value}${separator}v=${encodeURIComponent(String(version))}`;
    },

    isSubscriptionExpanded(uid) {
      return !!this.subscriptionExpanded[String(uid)];
    },

    toggleSubscriptionExpanded(uid) {
      const key = String(uid);
      this.subscriptionExpanded = {
        ...this.subscriptionExpanded,
        [key]: !this.subscriptionExpanded[key],
      };
    },

    toggleSubscriptionSection() {
      this.subscriptionSectionCollapsed = !this.subscriptionSectionCollapsed;
    },

    subscriptionThresholdSummary(item) {
      const value = Number(item.image_min_count);
      if (value === -1) {
        return "图片关闭";
      }
      return `阈值 ${Number.isFinite(value) ? value : 6}`;
    },

    subscriptionLivePhotoSummary(item) {
      return item.pull_livephoto ? "Live Photo 开" : "Live Photo 关";
    },

    subscriptionBadgeText(item) {
      if (String(item?.uid || "").startsWith("site:")) {
        return "站";
      }
      const source = String(item?.uname || item?.uid || "").trim();
      const chinese = source.match(/[\u4e00-\u9fff]/g);
      if (chinese && chinese.length) {
        return chinese[0];
      }
      const letters = source.replace(/[^a-zA-Z]/g, "").slice(0, 2);
      if (letters) {
        return letters.toUpperCase();
      }
      return source.slice(0, 1).toUpperCase() || "?";
    },

    async refreshGallery(reset = false, targetPage = null, direction = "replace", anchor = null) {
      galleryFetchController?.abort();
      galleryFetchController = typeof AbortController !== "undefined" ? new AbortController() : null;
      const fetchSignal = galleryFetchController?.signal;
      const requestId = ++this.galleryRequestId;
      this.galleryFetching = true;
      const renderToken = ++this.galleryRenderToken;
      this.cancelGalleryBatchWork();
      if (this.searchActive() && !String(this.searchQuery || "").trim()) {
        this.galleryLoading = false;
        this.galleryFetching = false;
        this.gallerySkeletonCount = 0;
        this.gallery = {
          ...this.gallery,
          items: [],
          total: 0,
          page: 1,
          total_pages: 1,
          view_mode: this.galleryViewMode,
        };
        this.displayedGalleryViewMode = this.galleryViewMode;
        this.$nextTick(() => this.observeGalleryLoadSentinel());
        return;
      }
      this.galleryLoading = reset;
      const pageSize = reset
        ? Math.max(Number(this.gallery.page_size || 0), this.preferredGalleryPageSize())
        : (Number(this.gallery.page_size) || this.preferredGalleryPageSize());
      const page = Math.max(1, Number(targetPage || (reset ? 1 : this.gallery.page) || 1));
      if (reset) {
        this.lockGalleryMinHeight();
        this.gallerySkeletonCount = (this.gallery.items || []).length ? 0 : Math.max(8, Math.min(12, pageSize));
        this.galleryEstimatedPageHeight = 980;
        this.gallery = { ...this.gallery, page: 1, page_size: pageSize, total_pages: 1 };
      } else if (direction === "next") {
        this.gallerySkeletonCount = 0;
      }
      if (this.randomModeActive() && !this.galleryRandomSeed) {
        this.galleryRandomSeed = this.newRandomSeed();
        localStorage.setItem("gallery_random_seed", this.galleryRandomSeed);
        this.galleryRandomExpired = false;
      } else if (!this.randomModeActive()) {
        this.galleryRandomExpired = false;
      }
      const params = new URLSearchParams({
        category: this.category === "search" ? "all" : this.category,
        view_mode: this.galleryViewMode,
        sort_order: this.sortOrder,
        page: String(page),
        page_size: String(pageSize),
      });
      if (this.randomModeActive()) params.set("random_seed", this.galleryRandomSeed);
      if (this.timeFilterApplied.startMonth) {
        params.set("start_month", this.timeFilterApplied.startMonth);
      }
      if (this.timeFilterApplied.endMonth) {
        params.set("end_month", this.timeFilterApplied.endMonth);
      }
      if (this.selectedSubscriptionUids.length) {
        params.set("subscription_uids", this.selectedSubscriptionUids.join(","));
      } else if (this.sourceKind !== "all") {
        params.set("source_kind", this.sourceKind);
      }
      if (this.category === "search" && this.searchQuery) {
        params.set("search_query", this.searchQuery);
      }
      const searchSignature = this.searchActive() ? this.searchSignature() : "";
      const localCacheKey = this.galleryLocalCacheKey(params);
      if (reset && page === 1) {
        const cached = this.readLocalGalleryCache(localCacheKey);
        if (cached) {
          if (searchSignature) {
            this.searchResultSignature = searchSignature;
            this.searchResultCount = Number(cached.total || 0);
          }
          this.applyGalleryPagePayload({ ...cached, cache_pending_refresh: true }, page, "replace", renderToken);
          this.galleryLoading = false;
        }
      }
      try {
        const payload = await this.api(`/api/gallery/items?${params.toString()}`, { signal: fetchSignal });
        if (requestId !== this.galleryRequestId) {
          return;
        }
        if (searchSignature && this.searchActive() && searchSignature === this.searchSignature()) {
          this.searchResultSignature = searchSignature;
          this.searchResultCount = Number(payload.total || 0);
        }
        this.writeLocalGalleryCache(localCacheKey, payload);
        this.applyGalleryPagePayload(payload, page, reset ? "replace" : direction, renderToken);
        this.$nextTick(() => {
          if (anchor && direction !== "next") {
            this.restoreGalleryAnchor(anchor);
          }
          if (this.autoLoadEnabled()) {
            this.handleScroll();
          }
        });
      } catch (error) {
        if (error.randomFeedExpired && requestId === this.galleryRequestId) this.galleryRandomExpired = true;
        if (error.name !== "AbortError") throw error;
      } finally {
        if (requestId === this.galleryRequestId) {
          this.galleryLoading = false;
          this.galleryFetching = false;
          if (reset) {
            this.releaseGalleryMinHeight();
          }
          if (!this.galleryBatchQueue.length) {
            this.gallerySkeletonCount = 0;
          }
        }
      }
    },

    galleryItemKey(item) {
      if (!item) {
        return "";
      }
      if (item.__deleted_placeholder) {
        return String(item.item_key || `deleted:${item.folder_name || item.__placeholder_id || "item"}`);
      }
      return String(item.item_key || item.folder_name || `${item.folder_name || "item"}::${item.pair_index || ""}`);
    },

    mergeGalleryItems(existingItems, incomingItems) {
      const output = [];
      const seen = new Set();
      [...existingItems, ...incomingItems].forEach((item) => {
        const key = this.galleryItemKey(item);
        if (!key || seen.has(key)) {
          return;
        }
        seen.add(key);
        output.push(item);
      });
      return output;
    },

    galleryPageNumbers() {
      const page = Number(this.gallery.page || 1);
      return Number.isFinite(page) && page > 0 ? [page] : [1];
    },

    firstLoadedGalleryPage() {
      return 1;
    },

    lastLoadedGalleryPage() {
      return Math.max(1, Number(this.gallery.page || 1));
    },

    galleryTotalPages() {
      const explicit = Number(this.gallery.total_pages);
      if (Number.isFinite(explicit) && explicit > 0) {
        return explicit;
      }
      const pageSize = Math.max(1, Number(this.gallery.page_size || 24));
      return Math.max(1, Math.ceil((Number(this.gallery.total) || 0) / pageSize));
    },

    flattenGalleryPages(pages = null) {
      return this.gallery.items || [];
    },

    visibleGalleryBatchSize() {
      return this.compactViewport ? 8 : 12;
    },

    preferredGalleryPageSize() {
      return Math.max(24, this.visibleGalleryBatchSize() * 3);
    },

    cancelGalleryBatchWork() {
      if (this.galleryBatchTimer) {
        window.clearTimeout(this.galleryBatchTimer);
        this.galleryBatchTimer = null;
      }
      this.galleryBatchQueue = [];
    },

    prepareGalleryItems(items, page, appearingKeys = null) {
      const pageSize = this.galleryWindowPageSize();
      return (items || []).map((item, index) => ({
        ...item,
        __gallery_page: page,
        __gallery_index: Math.max(0, (Number(page) - 1) * pageSize + index),
        __appearing: !this.galleryCardQuality[this.galleryItemKey(item)] && !(appearingKeys && appearingKeys.has(this.galleryItemKey(item))),
      }));
    },

    queueGalleryBatch(items, token) {
      if (token !== this.galleryRenderToken) {
        return;
      }
      this.galleryBatchQueue = [...(items || [])];
      this.gallerySkeletonCount = 0;
      const flush = () => {
        if (token !== this.galleryRenderToken) {
          this.galleryBatchQueue = [];
          this.galleryBatchTimer = null;
          return;
        }
        const batch = this.galleryBatchQueue.splice(0, this.visibleGalleryBatchSize());
        if (batch.length) {
          this.gallery = {
            ...this.gallery,
            items: this.mergeGalleryItems(this.gallery.items || [], batch),
          };
          this.galleryRenderEnd = Math.min(this.gallery.items.length, this.galleryRenderStart + this.galleryWindowSize());
        }
        this.$nextTick(() => {
          this.measureGalleryWindow();
          this.observeGalleryCards();
          this.observeGalleryLoadSentinel();
        });
        if (this.galleryBatchQueue.length) {
          this.galleryBatchTimer = window.setTimeout(flush, 70);
        } else {
          this.galleryBatchTimer = null;
        }
      };
      flush();
    },

    applyGalleryPagePayload(payload, page, direction, token = this.galleryRenderToken) {
      const incomingItems = payload.items || [];
      const sameMode = this.displayedGalleryViewMode === this.galleryViewMode;
      if (direction !== "next") {
        const fingerprint = JSON.stringify(incomingItems);
        const currentItems = this.gallery.items || [];
        if (
          sameMode && fingerprint
          && fingerprint === this.galleryPayloadFingerprint
          && (currentItems.length === incomingItems.length || this.galleryBatchQueue.length)
        ) {
          // 与当前正在渲染（或分批渲染中）的内容完全一致——典型场景是本地缓存先绘制、
          // 网络随后返回同样数据。保持 DOM 与图片质量进度原地不动，避免整批卡片
          // 销毁重建导致入场动画、渐进图淡入与质量预热整体重放（切页闪烁）。
          this.gallery = { ...this.gallery, ...payload, items: currentItems, page };
          return;
        }
        this.galleryPayloadFingerprint = fingerprint;
      }
      this.cancelGalleryBatchWork();
      const previousItems = sameMode ? (this.gallery.items || []) : [];
      const existingItems = direction === "next" ? previousItems : [];
      const previousByKey = new Map(previousItems.map(item => [this.galleryItemKey(item), item]));
      const prevKeys = direction === "next" ? null
        : new Set((this.gallery.items || []).map((item) => this.galleryItemKey(item)).filter(Boolean));
      const preparedItems = this.prepareGalleryItems(incomingItems, page, prevKeys).map(item => {
        const previous = previousByKey.get(this.galleryItemKey(item));
        if (!previous) return item;
        // Existing keys retain their DOM while metadata and changed media are updated.
        return { ...item, __appearing: false };
      });
      // Replace a page in one keyed update. Clearing first destroys Alpine nodes and replays image fades.
      const initialItems = direction === "next" ? existingItems : preparedItems;
      this.gallery = {
        ...payload,
        page,
        page_size: payload.page_size || this.gallery.page_size || 24,
        total_pages: payload.total_pages || this.gallery.total_pages || 1,
        items: initialItems,
      };
      if (direction !== "next") {
        this.displayedGalleryViewMode = this.galleryViewMode;
        this.galleryRenderStart = 0;
      }
      this.galleryRenderEnd = Math.min(initialItems.length, this.galleryRenderStart + this.galleryWindowSize());
      if (direction === "next") this.queueGalleryBatch(preparedItems, token);
      this.pruneGalleryWindowCaches();
      this.$nextTick(() => {
        this.measureGalleryWindow();
        this.observeGalleryCards();
        this.observeGalleryLoadSentinel();
        if (direction !== 'next' && this.currentView === 'gallery') this.fadeContent(document.querySelector('.gallery-layout'));
      });
    },

    galleryLocalCacheKey(params) {
      if (this.randomModeActive()) {
        return "";
      }
      return `gallery-page:creators-v1:${params.toString()}`;
    },

    readLocalGalleryCache(key) {
      if (!key) {
        return null;
      }
      try {
        const cached = JSON.parse(localStorage.getItem(key) || "null");
        return cached?.payload?.items && Date.now() - Number(cached.saved_at || 0) < 86400000 ? cached.payload : null;
      } catch (_error) {
        return null;
      }
    },

    writeLocalGalleryCache(key, payload) {
      if (!key || this.randomModeActive() || !payload?.items) {
        return;
      }
      const lightPayload = {
        ...payload,
        items: (payload.items || []).slice(0, Math.max(24, Number(payload.page_size || 24))),
      };
      this.scheduleIdleWork(() => {
        try {
          localStorage.setItem(key, JSON.stringify({ payload: lightPayload, saved_at: Date.now() }));
          const entries = [];
          for (let index = 0; index < localStorage.length; index++) {
            const candidate = localStorage.key(index);
            if (candidate?.startsWith("gallery-page:")) {
              try { entries.push([candidate, JSON.parse(localStorage.getItem(candidate)).saved_at || 0]); }
              catch (_) { localStorage.removeItem(candidate); }
            }
          }
          entries.sort((a, b) => b[1] - a[1]).slice(12).forEach(([candidate]) => localStorage.removeItem(candidate));
        } catch (_error) {
          return;
        }
      }, 1200);
    },

    clearLocalGalleryCaches() {
      try {
        for (let index = localStorage.length - 1; index >= 0; index -= 1) {
          const key = localStorage.key(index);
          if (key && key.startsWith("gallery-page:")) {
            localStorage.removeItem(key);
          }
        }
      } catch (_error) {
        return;
      }
    },

    cssEscape(value) {
      if (window.CSS && typeof window.CSS.escape === "function") {
        return window.CSS.escape(value);
      }
      return String(value).replace(/["\\]/g, "\\$&");
    },

    captureGalleryAnchor() {
      const viewportTop = window.scrollY || window.pageYOffset || 0;
      const viewportBottom = viewportTop + (window.innerHeight || 0);
      for (const item of this.gallery.items || []) {
        if (item.__deleted_placeholder) {
          continue;
        }
        const key = this.galleryItemKey(item);
        if (!key) {
          continue;
        }
        const element = document.querySelector(`[data-gallery-key="${this.cssEscape(key)}"]`);
        if (element) {
          const rect = element.getBoundingClientRect();
          const absoluteTop = rect.top + viewportTop;
          if (absoluteTop + rect.height >= viewportTop && absoluteTop <= viewportBottom) {
            return { key, top: rect.top };
          }
        }
      }
      const fallback = (this.gallery.items || []).find((item) => !item.__deleted_placeholder && this.galleryItemKey(item));
      if (fallback) {
        const key = this.galleryItemKey(fallback);
        const element = document.querySelector(`[data-gallery-key="${this.cssEscape(key)}"]`);
        if (element) {
          return { key, top: element.getBoundingClientRect().top };
        }
      }
      return null;
    },

    restoreGalleryAnchor(anchor) {
      if (!anchor?.key) {
        return;
      }
      this.$nextTick(() => {
        const element = document.querySelector(`[data-gallery-key="${this.cssEscape(anchor.key)}"]`);
        if (!element) {
          return;
        }
        const delta = element.getBoundingClientRect().top - Number(anchor.top || 0);
        if (Math.abs(delta) > 1) {
          window.scrollBy({ top: delta, left: 0, behavior: "instant" });
        }
      });
    },

    gallerySkeletonItems() {
      const count = Math.max(0, Number(this.gallerySkeletonCount || 0));
      return Array.from({ length: count }, (_item, index) => index + 1);
    },

    gallerySkeletonStyle(index) {
      const ratios = this.displayedGalleryViewMode === "pair" ? ["1 / 1", "4 / 5", "3 / 4"] : ["4 / 5", "1 / 1", "5 / 4"];
      return `aspect-ratio: ${ratios[(Number(index) - 1) % ratios.length]};`;
    },

    galleryPlaceholderStyle(item) {
      const height = Math.max(120, Number(item?.__placeholder_height || 0));
      const ratio = item?.display_ratio || "4 / 5";
      return { minHeight: `${height}px`, aspectRatio: ratio };
    },

    observeGalleryCards() {
      this.observeNarrowMasonry();
      if (typeof IntersectionObserver === "undefined") {
        const quality = {};
        this.galleryWindowItems().forEach((item) => {
          quality[this.galleryItemKey(item)] = "small";
        });
        this.galleryCardQuality = quality;
        return;
      }
      if (!this.gallerySmallObserver) {
        this.gallerySmallObserver = new IntersectionObserver((entries) => {
          entries.forEach((entry) => {
            if (!entry.isIntersecting) {
              return;
            }
            const key = entry.target.getAttribute("data-gallery-key");
            if (!key || entry.target.classList.contains("deleted-placeholder-card")) {
              return;
            }
            const current = this.galleryCardQuality[key] || "pending";
            if (this.galleryQualityRank(current) < this.galleryQualityRank("small")) {
              const item = (this.gallery.items || []).find((candidate) => this.galleryItemKey(candidate) === key);
              this.queueGallerySmallItem(item);
            }
          });
        }, { root: null, rootMargin: "100% 0px", threshold: 0 });
      }
      const nextQuality = { ...this.galleryCardQuality };
      const nextObserved = {};
      document.querySelectorAll(".gallery-item-card[data-gallery-key]").forEach((element) => {
        const key = element.getAttribute("data-gallery-key");
        if (!key) {
          return;
        }
        nextObserved[key] = element;
        if (nextQuality[key] === undefined) {
          nextQuality[key] = "pending";
        }
        const previous = this.galleryObservedCardElements[key];
        if (previous === element) {
          return;
        }
        if (previous) {
          this.gallerySmallObserver.unobserve(previous);
        }
        this.gallerySmallObserver.observe(element);
      });
      Object.entries(this.galleryObservedCardElements).forEach(([key, element]) => {
        if (nextObserved[key]) {
          return;
        }
        this.gallerySmallObserver.unobserve(element);
      });
      this.galleryObservedCardElements = nextObserved;
      this.galleryCardQuality = nextQuality;
      this.scheduleGallerySmallPromotion(160);
    },

    scheduleGallerySmallPromotion(delay = 220) {
      if (this.gallerySmallPromotionTimer) {
        return;
      }
      this.gallerySmallPromotionTimer = window.setTimeout(() => {
        this.gallerySmallPromotionTimer = null;
        if (this.galleryScrollSettling) {
          this.scheduleGallerySmallPromotion(80);
          return;
        }
        this.promoteVisibleGallerySmall();
      }, delay);
    },

    promoteVisibleGallerySmall() {
      if (this.currentView !== "gallery" || this.detail.open || this.viewer.open) {
        return;
      }
      const viewportHeight = window.innerHeight || 720;
      const topLimit = -viewportHeight * 1.1;
      const bottomLimit = viewportHeight * 2.1;
      const itemsByKey = new Map(
        this.galleryWindowItems().map((item) => [this.galleryItemKey(item), item]),
      );
      document.querySelectorAll(".gallery-item-card[data-gallery-key]").forEach((element) => {
        const key = element.getAttribute("data-gallery-key");
        if (!key || element.classList.contains("deleted-placeholder-card")) {
          return;
        }
        const rect = element.getBoundingClientRect();
        if (rect.bottom >= topLimit && rect.top <= bottomLimit) {
          const current = this.galleryCardQuality[key] || "pending";
          if (this.galleryQualityRank(current) < this.galleryQualityRank("small")) {
            this.queueGallerySmallItem(itemsByKey.get(key));
          }
        }
      });
    },

    observeGalleryLoadSentinel() {
      if (typeof IntersectionObserver === "undefined") {
        return;
      }
      const sentinel = document.querySelector("[data-gallery-load-sentinel]");
      if (!sentinel) {
        return;
      }
      if (this.galleryLoadObserver) return;
      this.galleryLoadObserver = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          this.scheduleGalleryLoadMore();
        }
      }, { root: null, rootMargin: "520px 0px", threshold: 0 });
      this.galleryLoadObserver.observe(sentinel);
    },

    scheduleGalleryLoadMore() {
      if (!this.autoLoadEnabled() || !this.galleryReadyForLoad() || !this.hasMoreItems()) {
        return;
      }
      if (this.galleryLoadTimer || this.loadingMore) {
        return;
      }
      this.galleryLoadTimer = window.setTimeout(() => {
        this.galleryLoadTimer = null;
        if (!this.autoLoadEnabled() || !this.galleryReadyForLoad() || !this.hasMoreItems()) {
          return;
        }
        this.loadMore().catch(() => {});
      }, 90);
    },

    async loadMore() {
      if (this.loadingMore || this.galleryFetching || !this.hasMoreItems()) {
        return;
      }
      const targetPage = this.lastLoadedGalleryPage() + 1;
      this.loadingMore = true;
      try {
        await this.refreshGallery(false, targetPage, "next", null);
      } finally {
        this.loadingMore = false;
      }
    },

    openGallery(category) {
      this.currentView = "gallery";
      this.category = category;
      if (category === "all") {
        this.timeSortMode = this.savedGalleryDefaultRandom ? "random" : this.orderedSortMode;
        this.sortOrder = this.timeSortMode;
      }
      if (category === "video") this.galleryViewMode = "folder";
      this.selectedSubscriptionUids = [];
      this.queuedCancelConfirmId = null;
      this.closeSidebarDrawer();
      this.closeGalleryLayers();
      this.timeFilterOpen = false;
      this.scrollViewTop();
      if (category === "search" && !this.searchQuery) {
        this.clearSearch();
        this.displayedGalleryViewMode = this.galleryViewMode;
        return;
      }
      this.refreshGalleryImmediately(true);
      this.scheduleIdleWork(() => this.refreshSidebarCounts([category]).catch(() => {}), 800);
    },

    openSubscriptions(panel = null) {
      this.closeGalleryLayers();
      this.currentView = "subscriptions";
      if (["overview", "up", "site", "xhs"].includes(panel)) {
        this.subscriptionPanel = panel;
        localStorage.setItem("subscription_panel", panel);
      }
      this.closeSidebarDrawer();
      this.scrollViewTop();
      if (this.subscriptionPanel === "xhs") {
        this.loadXhsSources();
      } else if (this.subscriptionPanel === "site") {
        this.refreshSites();
        this.refreshSidebarCounts(["subscriptions", "sites"]).catch(() => {});
      } else if (this.subscriptionPanel === "overview") {
        this.loadSubscriptions();
        this.refreshSites();
        this.refreshSidebarCounts(["subscriptions", "sites"]).catch(() => {});
      } else {
        this.loadSubscriptions();
        this.refreshSidebarCounts(["subscriptions"]).catch(() => {});
      }
    },

    openSites() {
      this.openSubscriptions("site");
    },

    openSettings() {
      this.closeGalleryLayers();
      this.currentView = "settings";
      this.closeSidebarDrawer();
      this.scrollViewTop();
      this.loadSettings();
      this.refreshTasks().catch(() => {});
    },

    openReview() {
      this.closeGalleryLayers();
      this.currentView = "review";
      this.closeSidebarDrawer();
      this.scrollViewTop();
      this.refreshReview();
      this.refreshSidebarCounts(["review"]).catch(() => {});
    },

    openDuplicates() {
      this.closeGalleryLayers();
      this.currentView = "duplicates";
      this.closeSidebarDrawer();
      this.scrollViewTop();
      this.refreshDuplicates();
    },

    openLogs() {
      this.closeGalleryLayers();
      this.currentView = "logs";
      this.closeSidebarDrawer();
      this.scrollViewTop();
      this.refreshLogs();
      this.refreshSidebarCounts(["logs"]).catch(() => {});
    },

    openRecentUpdates() {
      this.closeGalleryLayers();
      this.currentView = "recent-updates";
      this.closeSidebarDrawer();
      this.scrollViewTop();
      this.refreshRecentUpdates();
      this.$nextTick(() => this.scheduleRecentUpdatesMeasure());
    },

    selectSubscription(uid, sourceKindHint = null) {
      const normalized = String(uid);
      const subscription = (this.subscriptions || []).find((item) => String(item.uid) === normalized);
      const sourceKind = ["up", "site", "xhs"].includes(sourceKindHint) ? sourceKindHint
        : subscription?.source_type === "xhs_likes" ? "xhs" : normalized.startsWith("site:") ? "site" : "up";
      this.currentView = "gallery";
      this.category = "all";
      this.selectedSubscriptionUids = [normalized];
      this.sourceKind = sourceKind;
      localStorage.setItem("gallery_source_kind", this.sourceKind);
      this.queuedCancelConfirmId = null;
      this.closeSidebarDrawer();
      this.closeGalleryLayers();
      this.timeFilterOpen = false;
      this.scrollViewTop();
      this.refreshGalleryImmediately(true);
      this.scheduleIdleWork(() => this.refreshSidebarCounts(["all"]).catch(() => {}), 800);
    },

    toggleSubscriptionFilter(uid) {
      const normalized = String(uid);
      if (this.selectedSubscriptionUids.includes(normalized)) {
        this.selectedSubscriptionUids = this.selectedSubscriptionUids.filter((item) => item !== normalized);
      } else {
        this.selectedSubscriptionUids = [...this.selectedSubscriptionUids, normalized];
      }
      this.queuedCancelConfirmId = null;
      this.refreshGalleryImmediately(true);
    },

    subscriptionFilterActive(uid) {
      return this.selectedSubscriptionUids.includes(String(uid));
    },

    setSourceKind(kind) {
      if (!["all", "up", "site", "xhs"].includes(kind) || this.sourceKind === kind) {
        return;
      }
      this.sourceKind = kind;
      this.selectedSubscriptionUids = [];
      localStorage.setItem("gallery_source_kind", kind);
      this.closeGalleryLayers();
      this.scrollViewTop();
      if (this.currentView === "recent-updates") {
        this.refreshRecentUpdates();
        return;
      }
      this.refreshGalleryImmediately(true);
    },

    setSortOrder(order) {
      if (order !== "asc" && order !== "desc") {
        return;
      }
      this.setTimeSortMode(order);
    },

    setHoverPreviewEnabled(enabled) {
      this.hoverPreviewEnabled = !!enabled;
      localStorage.setItem("livephoto_hover_preview", this.hoverPreviewEnabled ? "1" : "0");
      if (!this.hoverPreviewEnabled) {
        document.querySelectorAll(".hover-preview-video").forEach((video) => {
          const card = video.closest(".asset-card, .detail-card");
          if (card) {
            this.stopHoverPreviewByCard(card);
          }
        });
      }
    },

    galleryPreloadDistance() {
      return Math.max(560, Math.round((window.innerHeight || 720) * 0.85));
    },

    galleryPageElement(page) {
      if (typeof document === "undefined" || !document.querySelector) {
        return null;
      }
      return document.querySelector(".gallery-virtual-page") || document.querySelector(".gallery-page");
    },

    isNearLoadedBottom() {
      const element = this.galleryPageElement(this.lastLoadedGalleryPage());
      if (!element) {
        return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 96;
      }
      const rect = element.getBoundingClientRect();
      return rect.bottom <= (window.innerHeight || 0) + this.galleryPreloadDistance();
    },

    hasMoreItems() {
      return !this.galleryRandomExpired && this.lastLoadedGalleryPage() < this.galleryTotalPages();
    },

    galleryReadyForLoad() {
      return (
        this.currentView === "gallery" &&
        !this.galleryFetching &&
        !this.detail.open &&
        !this.detailClosing &&
        !this.viewer.open &&
        !this.viewerClosing &&
        !this.taskInspector.open &&
        !this.galleryBatchQueue.length &&
        !this.galleryBatchTimer &&
        this.bodyLockTop === null &&
        this.hasMoreItems()
      );
    },

    newRandomSeed() {
      return typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    },

    mobileRandomRefresh() {
      return (this.compactViewport || this.narrowViewport) && window.matchMedia("(pointer: coarse)").matches;
    },

    async refreshRandomGallery() {
      if (!this.randomModeActive() || this.galleryFetching || this.randomRefreshing) return;
      this.galleryRandomSeed = this.newRandomSeed();
      localStorage.setItem("gallery_random_seed", this.galleryRandomSeed);
      this.galleryRandomExpired = false;
      this.randomRefreshing = true;
      this.scrollViewTop();
      try {
        await this.refreshGalleryImmediately(true);
      } finally {
        this.randomRefreshing = false;
      }
    },

    randomPullAllowed(trackpad = false) {
      return (this.mobileRandomRefresh() || (trackpad && (this.compactViewport || this.narrowViewport))) && this.currentView === "gallery" && this.randomModeActive()
        && !this.topOverlayLayer() && !this.sidebarDrawerOpen && !this.timeFilterOpen
        && !this.galleryFetching && !this.randomRefreshing && (window.scrollY || 0) <= 2;
    },

    randomPullThreshold() {
      return 160;
    },

    randomPullVisualProgress() {
      return this.mobileRandomRefresh() && this.randomModeActive()
        ? (this.randomRefreshing ? 1 : this.randomPullProgress) : 0;
    },

    startRandomPull(event) {
      this.cancelRandomPull();
      if (!this.randomPullAllowed() || event.touches.length !== 1
          || !event.target.closest?.(".workspace")
          || event.target.closest?.('button:not(.card-open-button), a, input, textarea, select, video, audio, iframe, [contenteditable]:not([contenteditable="false"]), [data-gesture-ignore]')
          || window.getSelection()?.toString()) return;
      for (let element = event.target; element && element !== document.body; element = element.parentElement) {
        if (element.scrollTop > 2) return;
      }
      const touch = event.touches[0];
      this.randomPullGesture = { id: touch.identifier, x: touch.clientX, y: touch.clientY, locked: false,
        location: this.touchBackContextKey() };
    },

    moveRandomPull(event) {
      const gesture = this.randomPullGesture;
      if (!gesture) return;
      if (!this.randomPullAllowed() || event.touches.length !== 1
          || gesture.location !== this.touchBackContextKey()) { this.cancelRandomPull(); return; }
      const touch = event.touches[0];
      if (touch.identifier !== gesture.id) { this.cancelRandomPull(); return; }
      const dx = touch.clientX - gesture.x, dy = touch.clientY - gesture.y;
      if (!gesture.locked) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) < 12) return;
        if (dy <= 0 || dy < Math.abs(dx) * 1.4) { this.cancelRandomPull(); return; }
        gesture.locked = true;
      }
      if (event.cancelable) event.preventDefault();
      this.randomPullProgress = Math.max(0, Math.min(1, dy / this.randomPullThreshold()));
    },

    endRandomPull(event) {
      const gesture = this.randomPullGesture;
      const touch = event.changedTouches?.[0];
      const ready = gesture?.locked && !event.touches.length && this.randomPullProgress >= 1 && touch?.identifier === gesture.id
        && touch.clientY - gesture.y >= this.randomPullThreshold() && this.randomPullAllowed()
        && touch.clientY - gesture.y > Math.abs(touch.clientX - gesture.x) * 1.4
        && gesture.location === this.touchBackContextKey();
      this.cancelRandomPull();
      if (!ready) return;
      if (event.cancelable) event.preventDefault();
      this.touchBackClickUntil = Date.now() + 450;
      this.refreshRandomGallery();
    },

    cancelRandomPull() {
      this.randomPullGesture = null;
      this.randomPullProgress = 0;
    },

    randomModeActive() {
      return this.timeSortMode === "random";
    },

    autoLoadEnabled() {
      return !!this.settings.auto_load_enabled;
    },

    showManualLoadMore() {
      return this.galleryReadyForLoad() && this.hasMoreItems() && !this.autoLoadEnabled();
    },

    handleScroll() {
      if (!this.autoLoadEnabled() || !this.galleryReadyForLoad()) {
        return;
      }
      const nearLoadedBottom = this.hasMoreItems() && this.isNearLoadedBottom();
      if (nearLoadedBottom) {
        this.scheduleGalleryLoadMore();
      }
    },

    galleryLoadFinished() {
      return !this.galleryRandomExpired && this.gallery.items.length > 0 && !this.hasMoreItems();
    },

    isStandaloneVideoFolder(folder, folderName = "") {
      return Number(folder?.video_count || folder?.videos?.length || 0) > 0;
    },

    galleryPreviewTiles(item) {
      const tiles = item?.preview_tiles || [];
      if (this.isStandaloneVideoFolder(item)) return tiles.slice(0, 1);
      if (!this.narrowViewport && !this.settings.wide_single_cover) return tiles.slice(0, 4);
      if (this.settings.random_cover_enabled !== false && item?.random_cover) return [item.random_cover];
      return tiles.slice(0, 1);
    },

    galleryCoverStyle(item) {
      if (!this.narrowViewport && !this.settings.wide_single_cover) return null;
      const tile = this.galleryPreviewTiles(item)[0];
      const ratio = tile?.width > 0 && tile?.height > 0 ? tile.width / tile.height : 3 / 4;
      return `aspect-ratio: ${Math.max(.65, Math.min(1.3, ratio))};`;
    },

    observeNarrowMasonry() {
      if (typeof document === 'undefined') return;
      const cards = document.querySelectorAll('.gallery-page .gallery-item-card');
      if (!this.narrowViewport || typeof ResizeObserver === "undefined") {
        masonryObserver?.disconnect();
        masonryElements.clear();
        cards.forEach(card => {
          card.style.removeProperty('grid-row-end');
          card.removeAttribute('data-masonry-measured');
        });
        return;
      }
      const measure = card => {
        // Measure the whole border box, including Live Photo borders and deletion placeholders.
        // Hidden cards retain their last span until ResizeObserver sees them again.
        if (!card.offsetWidth || !card.offsetHeight) return;
        card.style.gridRowEnd = `span ${Math.ceil((card.offsetHeight + 8) / 12)}`;
        card.setAttribute('data-masonry-measured', '');
      };
      if (!masonryObserver) masonryObserver = new ResizeObserver(entries => entries.forEach(entry => measure(entry.target)));
      const nextElements = new Set(cards);
      cards.forEach(card => {
        measure(card);
        if (!masonryElements.has(card)) masonryObserver.observe(card, { box: 'border-box' });
      });
      for (const content of masonryElements) if (!nextElements.has(content)) masonryObserver.unobserve(content);
      masonryElements.clear();
      nextElements.forEach(content => masonryElements.add(content));
    },

    prepareVideoFeed(folder) {
      if (this.currentView !== 'gallery') return;
      const seen = new Set();
      const items = [...(this.gallery.items || []), ...(this.galleryBatchQueue || [])]
        .filter(item => !item.__deleted_placeholder && this.isStandaloneVideoFolder(item))
        .filter(item => !seen.has(item.folder_name) && seen.add(item.folder_name));
      if (!items.some(item => item.folder_name === folder.folder_name)) items.push(folder);
      const params = new URLSearchParams({
        category: this.category === 'search' ? 'all' : this.category,
        view_mode: 'folder', sort_order: this.sortOrder,
        page_size: String(this.gallery.page_size || this.preferredGalleryPageSize()),
      });
      if (this.randomModeActive()) params.set('random_seed', this.galleryRandomSeed);
      if (this.timeFilterApplied.startMonth) params.set('start_month', this.timeFilterApplied.startMonth);
      if (this.timeFilterApplied.endMonth) params.set('end_month', this.timeFilterApplied.endMonth);
      if (this.selectedSubscriptionUids.length) params.set('subscription_uids', this.selectedSubscriptionUids.join(','));
      else if (this.sourceKind !== 'all') params.set('source_kind', this.sourceKind);
      if (this.category === 'search' && this.searchQuery) params.set('search_query', this.searchQuery);
      this.videoFeed = { active: true, items, index: items.findIndex(item => item.folder_name === folder.folder_name),
        page: this.lastLoadedGalleryPage(), totalPages: this.galleryTotalPages(), params: params.toString(), busy: false };
      this.videoFeedMessage = '';
    },

    async stepVideoFeed(direction) {
      if (!this.videoNote.open || this.videoNote.loading || !this.videoNote.video || !this.videoFeed.active || this.videoFeed.busy) return;
      const token = ++this.videoFeedToken;
      this.videoFeed.busy = true;
      this.videoFeedPositionTotal = this.videoFeed.items.length;
      this.videoFeedMessage = '';
      this.videoFeedDirection = direction;
      try {
        const index = this.videoFeed.index + direction;
        while (index >= this.videoFeed.items.length && this.videoFeed.page < this.videoFeed.totalPages) {
          const page = this.videoFeed.page + 1;
          const params = new URLSearchParams(this.videoFeed.params);
          params.set('page', String(page));
          const payload = await this.api(`/api/gallery/items?${params}`);
          if (token !== this.videoFeedToken || !this.videoNote.open) return;
          const seen = new Set(this.videoFeed.items.map(item => item.folder_name));
          const items = (payload.items || []).filter(item => this.isStandaloneVideoFolder(item) && !seen.has(item.folder_name) && seen.add(item.folder_name));
          this.videoFeed.items.push(...items);
          this.videoFeed.page = page;
          this.videoFeed.totalPages = Number(payload.total_pages || page);
        }
        if (index < 0 || index >= this.videoFeed.items.length) {
          this.videoFeedMessage = direction > 0 ? '已经是最后一条视频' : '已经是第一条视频';
          return;
        }
        const item = this.videoFeed.items[index];
        const payload = this.cachedDetail(item.folder_name) || await this.api(`/api/gallery/folders/${encodeURIComponent(item.folder_name)}`);
        if (token !== this.videoFeedToken || !this.videoNote.open) return;
        if (!payload.videos?.length) {
          this.videoFeedMessage = '这条视频暂时无法播放，请重试或切换下一条';
          await this.fadeVideoFeedCaption(() => {
            this.videoFeed.items.splice(index, 1);
            if (direction < 0) this.videoFeed.index -= 1;
            this.videoFeedPositionTotal = null;
          }, token);
          return;
        }
        this.cacheDetailPayload(item.folder_name, payload);
        this.pendingTrashFolder = null;
        // Mount the next player behind the current frame. Commit only after it can render.
        this.videoFeedPending = { payload, index, token };
        this.videoFeedReadyTimer = window.setTimeout(() => this.failPendingVideo(token), 15000);
        this.$nextTick(() => {
          const video = document.querySelector('.xhs-video-shell video:not(.is-retiring):not(.is-current)') || this.currentVideoNotePlayer();
          if (video?.readyState >= 2) this.videoNotePlayerReady({ key: this.videoNotePlayerKey(payload.folder, payload.videos[0]) });
        });
      } catch (error) {
        if (token === this.videoFeedToken) this.videoFeedMessage = error.message || '读取视频失败，请重试';
      } finally {
        if (token === this.videoFeedToken && !this.videoFeedPending) {
          if (this.videoFeedPositionTotal !== null && this.videoFeedPositionTotal !== this.videoFeed.items.length) {
            await this.fadeVideoFeedCaption(() => { this.videoFeedPositionTotal = null; }, token);
          }
          if (token !== this.videoFeedToken) return;
          this.videoFeed.busy = false;
          this.videoFeedPositionTotal = null;
        }
      }
    },

    videoNotePlayerKey(folder = this.videoNote.folder, video = this.videoNote.video) {
      return `${folder?.folder_name || ''}::${video?.url || ''}`;
    },

    videoFeedPositionText() {
      const total = this.videoFeedPositionTotal ?? this.videoFeed.items.length;
      return `${this.videoFeed.index + 1} / ${total}${this.videoFeed.page < this.videoFeed.totalPages ? '+' : ''}`;
    },

    videoNotePlayers() {
      if (!this.videoNote.open && !this.videoNoteClosing) return [];
      const players = [];
      if (this.videoFeedRetiring) players.push(this.videoFeedRetiring);
      if (this.videoNote.video) players.push({ key: this.videoNotePlayerKey(), video: this.videoNote.video });
      const pending = this.videoFeedPending?.payload;
      if (pending) {
        const key = this.videoNotePlayerKey(pending.folder, pending.videos[0]);
        if (!players.some(player => player.key === key)) players.push({ key, video: pending.videos[0] });
      }
      return players;
    },

    currentVideoNotePlayer() {
      return document.querySelector('.xhs-video-shell video.is-current');
    },

    async toggleVideoNoteFullscreen() {
      const shell = document.querySelector('.xhs-video-shell');
      if (!shell || !this.videoNote.open) return;
      if (this.currentFullscreenElement()) { this.exitCurrentFullscreen(); return; }
      try {
        if (shell.requestFullscreen) await shell.requestFullscreen();
        else if (shell.webkitRequestFullscreen) await shell.webkitRequestFullscreen();
        else this.videoNote.error = '此浏览器暂不支持整页全屏，可使用播放器的全屏按钮。';
      } catch (_error) {
        this.videoNote.error = '全屏未能打开，请再次点击全屏按钮。';
      }
    },

    async failPendingVideo(token) {
      if (token !== this.videoFeedToken || !this.videoFeedPending) return;
      window.clearTimeout(this.videoFeedReadyTimer);
      this.videoFeedReadyTimer = null;
      this.videoFeedPending = null;
      this.videoFeedMessage = '这条视频加载失败，请重试或切换其他视频';
      if (this.videoFeedPositionTotal !== null && this.videoFeedPositionTotal !== this.videoFeed.items.length) {
        await this.fadeVideoFeedCaption(() => { this.videoFeedPositionTotal = null; }, token);
      }
      if (token !== this.videoFeedToken) return;
      this.videoFeedSwitching = false;
      this.videoFeed.busy = false;
      this.videoFeedPositionTotal = null;
    },

    async fadeVideoFeedCaption(update, token) {
      this.videoFeedSwitching = true;
      if (!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
        await new Promise(resolve => window.setTimeout(resolve, 120));
      }
      if (token !== this.videoFeedToken || !this.videoNote.open) return false;
      update();
      await this.$nextTick();
      if (token !== this.videoFeedToken || !this.videoNote.open) return false;
      this.videoFeedSwitching = false;
      return true;
    },

    async videoNotePlayerReady(player) {
      const pending = this.videoFeedPending;
      if (!pending || pending.committing || player.key !== this.videoNotePlayerKey(pending.payload.folder, pending.payload.videos[0])) return;
      pending.committing = true;
      window.clearTimeout(this.videoFeedReadyTimer);
      this.videoFeedReadyTimer = null;
      this.videoFeedSwitching = true;
      const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
      if (!reduced) await new Promise(resolve => window.setTimeout(resolve, 120));
      if (pending.token !== this.videoFeedToken || this.videoFeedPending !== pending || !this.videoNote.open) return;
      this.currentVideoNotePlayer()?.pause();
      this.videoFeedRetiring = this.videoNote.video ? { key: this.videoNotePlayerKey(), video: this.videoNote.video } : null;
      this.videoFeed.index = pending.index;
      this.videoFeedPositionTotal = null;
      this.videoNote = { open: true, loading: false, folder: pending.payload.folder, video: pending.payload.videos[0], error: '' };
      this.videoFeedPending = null;
      await this.$nextTick();
      if (pending.token !== this.videoFeedToken || !this.videoNote.open) return;
      this.videoFeedSwitching = false;
      this.videoFeed.busy = false;
      this.startVideoNote();
      if (this.narrowViewport) this.showVideoFeedHint();
      window.clearTimeout(this.videoFeedRetireTimer);
      this.videoFeedRetireTimer = window.setTimeout(() => {
        this.videoFeedRetiring = null;
        this.videoFeedRetireTimer = null;
      }, reduced ? 0 : 240);
    },

    videoNotePlayerFailed(player) {
      const pending = this.videoFeedPending;
      if (pending && player.key === this.videoNotePlayerKey(pending.payload.folder, pending.payload.videos[0])) {
        this.failPendingVideo(pending.token);
      } else if (player.key === this.videoNotePlayerKey()) {
        this.videoNote.error = '视频无法播放，请检查归档文件或重试媒体。';
      }
    },

    startVideoFeedGesture(event) {
      this.videoFeedGesture = null;
      if (!(this.compactViewport || this.narrowViewport) || !this.videoFeed.active || event.touches.length !== 1 || event.target.closest('button, a, input, header, .video-feed-caption')) return;
      const touch = event.touches[0];
      const player = this.currentVideoNotePlayer();
      if (player && touch.clientY > player.getBoundingClientRect().bottom - 72) return;
      this.videoFeedGesture = { x: touch.clientX, y: touch.clientY };
    },

    moveVideoFeedGesture(event) {
      if (event.touches.length !== 1) { this.videoFeedGesture = null; return; }
      if (!this.videoFeedGesture) return;
      const dy = event.touches[0].clientY - this.videoFeedGesture.y;
      const dx = event.touches[0].clientX - this.videoFeedGesture.x;
      if (Math.abs(dy) > 12 && Math.abs(dy) > Math.abs(dx) && event.cancelable) event.preventDefault();
    },

    endVideoFeedGesture(event) {
      const start = this.videoFeedGesture;
      this.videoFeedGesture = null;
      if (!start || !event.changedTouches.length) return;
      const dx = event.changedTouches[0].clientX - start.x;
      const dy = event.changedTouches[0].clientY - start.y;
      if (Math.abs(dy) >= 60 && Math.abs(dy) > Math.abs(dx) * 1.4) this.stepVideoFeed(dy < 0 ? 1 : -1);
    },

    async openFolder(folderName, previewOverride = null) {
      detailFetchController?.abort();
      detailFetchController = null;
      this.detailError = "";
      this.closeVideoNote();
      if (this.detailCloseTimer) {
        window.clearTimeout(this.detailCloseTimer);
        this.detailCloseTimer = null;
      }
      const resetDetailScroll = () => this.$nextTick(() => this.scrollDetailTop());
      this.detailTextExpanded = false;
      this.detailTextOverflowing = false;
      this.setInteracting(360);
      this.cancelDeleteViewerPair();
      this.pauseCurrentViewerMedia(true);
      this.detailClosing = false;
      this.detailRequestId += 1;
      const requestId = this.detailRequestId;
      const previewFolder = previewOverride
        || (this.gallery.items || []).find((item) => item.folder_name === folderName)
        || (this.recentUpdates.groups || []).flatMap((group) => group.items || []).find((item) => item.folder_name === folderName)
        || (this.duplicateGroups || []).flatMap((group) => group.items || []).find((item) => item.folder_name === folderName)
        || (this.detail.folder?.folder_name === folderName ? this.detail.folder : null);
      const likelyVideo = this.isStandaloneVideoFolder(previewFolder, folderName);
      if (likelyVideo) this.prepareVideoFeed(previewFolder);
      const cached = this.cachedDetail(folderName);
      if (cached && (cached.pairs?.length || cached.videos?.length)) {
        if (this.openVideoNote(cached)) return;
        if (!likelyVideo) {
          this.detailLoading = false;
          this.detail = {
            ...cached,
            open: true,
            folder: previewFolder ? { ...cached.folder, ...previewFolder } : cached.folder,
          };
          this.resetDetailWindow();
          this.syncBodyLock();
          resetDetailScroll();
          return;
        }
      }

      this.detailLoading = true;
      if (likelyVideo) {
        this.openVideoNoteLoading(previewFolder || cached?.folder || { folder_name: folderName, title: folderName });
      } else {
        const previewPairs = this.detailPreviewPairs(folderName, previewFolder);
        this.detail = {
          open: true,
          pairs: this.detail.folder?.folder_name === folderName && this.detail.pairs?.length ? this.detail.pairs : previewPairs,
          folder: previewFolder || { folder_name: folderName, title: folderName },
          videos: this.detail.folder?.folder_name === folderName ? (this.detail.videos || []) : [],
        };
        this.resetDetailWindow();
        this.syncBodyLock();
        resetDetailScroll();
      }
      if (requestId !== this.detailRequestId) {
        return;
      }
      const controller = new AbortController();
      detailFetchController = controller;
      const timeout = window.setTimeout(() => controller.abort(), 20000);
      try {
        const payload = await this.api(`/api/gallery/folders/${encodeURIComponent(folderName)}`, { signal: controller.signal });
        if (requestId !== this.detailRequestId) {
          return;
        }
        this.detailLoading = false;
        if (this.openVideoNote(payload)) {
          this.cacheDetailPayload(folderName, payload);
          return;
        }
        if (likelyVideo || this.isStandaloneVideoFolder(payload.folder, folderName)) {
          if (!this.videoNote.open || this.videoNote.folder?.folder_name !== folderName) {
            this.openVideoNoteLoading(payload.folder || previewFolder || { folder_name: folderName, title: folderName });
          }
          this.videoNote.folder = payload.folder || this.videoNote.folder;
          this.videoNote.loading = false;
          this.videoNote.error = "视频尚未成功归档，或详情中没有可播放的视频文件。请重试媒体后再打开。";
          this.syncBodyLock();
          return;
        }
        if (this.videoNote.open && this.videoNote.folder?.folder_name === folderName) {
          this.closeVideoNote();
        }
        const detailPairs = this.normalizeDetailPairs(payload.pairs || [], false, this.detail.pairs || []);
        if (!detailPairs.length) this.detailError = "当前动态没有可显示的图片。";
        const applyDetailPayload = () => {
          this.detail = {
            open: true,
            pairs: detailPairs,
            folder: payload.folder,
            videos: payload.videos || [],
          };
          this.cacheDetailPayload(folderName, this.detail);
          this.resetDetailWindow();
          this.syncBodyLock();
          resetDetailScroll();
        };
        if ((this.detail.pairs || []).length) {
          this.animateDetailReflow(applyDetailPayload);
        } else {
          applyDetailPayload();
        }
      } catch (error) {
        if (requestId !== this.detailRequestId) {
          return;
        }
        this.detailLoading = false;
        if (this.videoNote.open && this.videoNote.folder?.folder_name === folderName) {
          this.videoNote.loading = false;
          this.videoNote.error = "读取视频详情失败，请检查归档文件或重试媒体。";
          return;
        }
        this.detailError = controller.signal.aborted ? "详情加载超时，请重试。" : "详情加载失败，请重试。";
      } finally {
        window.clearTimeout(timeout);
        if (detailFetchController === controller) detailFetchController = null;
      }
    },

    openVideoNoteLoading(folder) {
      this.cancelVideoNoteClose();
      this.closeDetail(true);
      this.pauseCurrentViewerMedia(true);
      this.detail = { open: false, pairs: [], folder: folder || null, videos: [] };
      this.videoNote = { open: true, loading: true, folder: folder || null, video: null, error: '' };
      this.syncBodyLock();
    },

    openVideoNote(payload) {
      const folder = payload?.folder;
      const isXhs = String(folder?.folder_name || '').startsWith('xhs_');
      const standalone = this.isStandaloneVideoFolder(folder)
        || (isXhs && payload?.videos?.length > 0 && !folder?.has_livephoto);
      if (!standalone || !payload?.videos?.length) return false;
      this.cancelVideoNoteClose();
      this.closeDetail(true);
      this.pauseCurrentViewerMedia(true);
      this.videoNote = { open: true, loading: false, folder, video: payload.videos[0], error: '' };
      if (this.narrowViewport && this.videoFeed.active && !this.videoFeed.hintShown) {
        this.videoFeed.hintShown = true;
        this.showVideoFeedHint();
      }
      this.syncBodyLock();
      this.$nextTick(() => this.startVideoNote());
      return true;
    },

    showVideoFeedHint() {
      if (this.videoFeedHintTimer) window.clearTimeout(this.videoFeedHintTimer);
      this.videoFeedHintVisible = true;
      this.videoFeedHintTimer = window.setTimeout(() => {
        this.videoFeedHintVisible = false;
        this.videoFeedHintTimer = null;
      }, 2000);
    },

    startVideoNote() {
      const video = this.currentVideoNotePlayer();
      if (!video || !this.videoNote.open) return;
      const configuredVolume = Number(this.settings?.video_default_volume);
      const volumePercent = Number.isFinite(configuredVolume) ? configuredVolume : 0;
      const volume = Math.max(0, Math.min(100, volumePercent)) / 100;
      video.loop = this.settings?.video_default_loop !== false;
      video.volume = volume;
      video.muted = volume <= 0;
      video.defaultMuted = video.muted;
      const playing = video.play();
      playing?.catch(() => {
        if (this.videoNote.open && video === this.currentVideoNotePlayer())
          this.videoNote.error = volume > 0
            ? '浏览器阻止了带声音的自动播放，请点击播放器中的播放按钮。'
            : '自动播放未能开始，请点击播放器中的播放按钮。';
      });
    },

    cancelVideoNoteClose() {
      window.clearTimeout(this.videoNoteCloseTimer);
      this.videoNoteCloseTimer = null;
      this.videoNoteClosing = false;
    },

    closeVideoNote(skipAnimation = false) {
      if (!this.videoNote.open && !this.videoNoteClosing) return;
      this.cancelVideoNoteClose();
      if (this.videoNoteFullscreen) this.exitCurrentFullscreen();
      this.videoNoteFullscreen = false;
      window.clearTimeout(this.videoFeedReadyTimer);
      window.clearTimeout(this.videoFeedRetireTimer);
      this.videoFeedReadyTimer = null;
      this.videoFeedRetireTimer = null;
      this.videoFeedPending = null;
      this.videoFeedRetiring = null;
      this.videoFeedSwitching = false;
      this.videoFeedPositionTotal = null;
      if (this.videoFeedHintTimer) window.clearTimeout(this.videoFeedHintTimer);
      this.videoFeedHintTimer = null;
      this.videoFeedHintVisible = false;
      this.pendingTrashFolder = null;
      this.detailRequestId += 1;
      this.videoFeedToken += 1;
      this.videoFeed.active = false;
      this.videoFeed.busy = false;
      this.videoFeedGesture = null;
      this.videoFeedMessage = '';
      const video = this.currentVideoNotePlayer();
      video?.pause();
      this.videoNoteClosing = !skipAnimation && !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
      this.videoNote = { ...this.videoNote, open: false, loading: false, error: '' };
      const finish = () => {
        if (this.videoNote.open) return;
        video?.removeAttribute('src');
        video?.load();
        this.videoNote = { open: false, loading: false, folder: null, video: null, error: '' };
        this.videoNoteClosing = false;
        this.videoNoteCloseTimer = null;
        this.syncBodyLock();
      };
      if (this.videoNoteClosing) this.videoNoteCloseTimer = window.setTimeout(finish, 240);
      else finish();
      this.syncBodyLock();
    },

    galleryFolderSequence() {
      if (this.currentView === "duplicates") {
        return [...new Set(
          (this.duplicateGroups || [])
            .flatMap((group) => group.items || [])
            .map((item) => String(item?.folder_name || ""))
            .filter(Boolean),
        )];
      }
      const output = [];
      const seen = new Set();
      for (const item of this.gallery.items || []) {
        const folderName = String(item?.folder_name || "");
        if (!folderName || item.__deleted_placeholder || seen.has(folderName)) {
          continue;
        }
        seen.add(folderName);
        output.push(folderName);
      }
      return output;
    },

    currentDetailFolderIndex(sequence = this.galleryFolderSequence()) {
      const folderName = this.detail.folder?.folder_name;
      if (!folderName) {
        return -1;
      }
      return (sequence || []).findIndex((item) => item === folderName);
    },

    canShowPreviousFolder() {
      return this.detail.open && !this.detailLoading && this.currentDetailFolderIndex() > 0;
    },

    canShowNextFolder() {
      const sequence = this.galleryFolderSequence();
      const index = this.currentDetailFolderIndex(sequence);
      return this.detail.open && !this.detailLoading && index >= 0 && index < sequence.length - 1;
    },

    showPreviousFolder() {
      const sequence = this.galleryFolderSequence();
      const index = this.currentDetailFolderIndex(sequence);
      if (index <= 0) {
        return;
      }
      this.openFolder(sequence[index - 1]);
    },

    showNextFolder() {
      const sequence = this.galleryFolderSequence();
      const index = this.currentDetailFolderIndex(sequence);
      if (index < 0 || index >= sequence.length - 1) {
        return;
      }
      this.openFolder(sequence[index + 1]);
    },

    detailPreviewPairs(folderName, previewFolder) {
      if (this.galleryViewMode === "pair") {
        return (this.gallery.items || [])
          .filter((item) => item.folder_name === folderName)
          .map((item) => ({
            pair_index: Number(item.pair_index) || 0,
            image: item.image || {
              small_thumb_url: item.small_thumb_url,
              thumb_url: item.thumb_url,
              url: item.preview_url,
              width: item.width,
              height: item.height,
            },
            livephoto: item.livephoto || null,
            preview_url: this.galleryPairImageUrl(item),
            preview_kind: item.preview_kind || (item.has_livephoto ? "paired" : "image"),
            complete: !!(item.has_images && item.has_livephoto),
            display_ratio: item.display_ratio || "1 / 1",
            promote_preview: false,
          }));
      }
      const seenIndices = new Set();
      const tiles = (previewFolder?.preview_tiles || []).map((tile, index) => ({
        pair_index: Number(tile.pair_index) || index + 1,
        image: {
          ...tile,
          url: this.galleryTileUrl(tile),
        },
        livephoto: null,
        preview_url: this.galleryTileUrl(tile),
        preview_kind: "image",
        complete: false,
        display_ratio: this.assetRatio(tile),
        promote_preview: false,
        placeholder: !this.galleryTileUrl(tile),
      })).filter(tile => {
        if (seenIndices.has(tile.pair_index)) return false;
        seenIndices.add(tile.pair_index);
        return true;
      });
      const fallbackRatios = tiles.map((tile) => tile.display_ratio).filter(Boolean);
      const fallbackRatio = fallbackRatios[0] || "1 / 1";
      const expectedCount = Math.max(
        tiles.length,
        Number(previewFolder?.image_count || 0) || 0,
        Number(previewFolder?.livephoto_count || 0) || 0,
        Number(previewFolder?.asset_count || 0) || 0,
        previewFolder ? 0 : this.resolveDetailColumns(),
      );
      let nextIndex = 1;
      for (let index = tiles.length; index < expectedCount; index += 1) {
        while (seenIndices.has(nextIndex)) nextIndex += 1;
        const pairIndex = nextIndex++;
        seenIndices.add(pairIndex);
        const displayRatio = fallbackRatios[index % Math.max(1, fallbackRatios.length)] || fallbackRatio;
        tiles.push({
          pair_index: pairIndex,
          image: null,
          livephoto: null,
          preview_url: "",
          preview_kind: "placeholder",
          complete: false,
          display_ratio: displayRatio,
          promote_preview: false,
          placeholder: true,
        });
      }
      return tiles;
    },

    normalizeDetailPairs(pairs, promote = false, previousPairs = []) {
      const previousByIndex = new Map(
        (previousPairs || []).map((pair) => [Number(pair?.pair_index || 0), pair]),
      );
      return (pairs || []).map((pair) => {
        const previous = previousByIndex.get(Number(pair?.pair_index || 0));
        return {
          ...pair,
          promote_preview: !!promote || !!previous?.promote_preview,
          __appearing: !previous,
        };
      });
    },

    assetRatio(asset) {
      const width = Number(asset?.width || 0);
      const height = Number(asset?.height || 0);
      if (width > 0 && height > 0) {
        return `${width} / ${height}`;
      }
      return "1 / 1";
    },

    detailPairPreviewUrl(pair) {
      if (pair?.placeholder) {
        return "";
      }
      return pickThumbUrl([pair?.image, pair?.livephoto]);
    },

    openPair(pairIndex) {
      const sequence = this.buildDetailViewerSequence();
      const index = sequence.findIndex((item) => this.viewerEntryKey(item) === `${this.detail.folder?.folder_name}::${pairIndex}`);
      if (index < 0) return;
      this.viewerSource = "detail";
      this.viewerSequence = sequence;
      this.viewerIndex = index;
      this.openViewerEntry(sequence[index]);
    },

    openViewerEntry(entry, preservePlayback = false, startImmediately = true, switchDirection = "") {
      if (!entry?.pair) {
        return;
      }
      if (!this.viewer.open) this.viewerToolbarOpen = false;
      if (this.viewerCloseTimer) {
        window.clearTimeout(this.viewerCloseTimer);
        this.viewerCloseTimer = null;
      }
      this.viewerClosing = false;
      this.pauseCurrentViewerMedia(true);
      const pair = entry.pair;
      if (this.viewerHandoff?.key !== this.viewerEntryKey(entry)) this.viewerHandoff = null;
      this.viewerToken += 1;
      const shouldShowVideo = !!pair.livephoto && !pair.image;
      this.cancelViewerPreviewRetirement();
      this.viewerPreviewRetired = false;
      this.viewerImageReady = !pair.image;
      this.loadViewerOriginal(pair.image?.url || "");
      this.viewerImageRevealAfter = Date.now();
      if (this.viewerImageRevealTimer) {
        window.clearTimeout(this.viewerImageRevealTimer);
        this.viewerImageRevealTimer = null;
      }
      this.viewerSwapPending = false;
      this.viewerPendingDirection = "";
      this.viewerNavigationLockedUntil = switchDirection ? Date.now() + 80 : 0;
      this.viewer = { open: true, pair, folder: entry.folder, showVideo: false };
      this.showViewerPositionHint();
      this.cancelDeleteViewerPair();
      if (!preservePlayback) {
        this.playbackMode = "once";
        this.scheduleIdleWork(() => localStorage.setItem("livephoto_playback_mode", this.playbackMode), 500);
      }
      this.resetViewerTransform();
      this.setInteracting(320);
      this.syncBodyLock();
      const viewerToken = this.viewerToken;
      this.$nextTick(() => {
        if (!this.viewer.open || viewerToken !== this.viewerToken) return;
        if (switchDirection) this.animateViewerSwitch(switchDirection);
        const image = document.querySelector(".viewer-stage-image");
        if (pair.image && image?.complete && image.naturalWidth > 0
            && image.dataset.viewerToken === String(this.viewerToken)) {
          this.viewerMediaLoaded("image", { target: image });
        }
        if (shouldShowVideo) {
          this.scheduleViewerPlaybackStart(260, true);
        } else if (pair.livephoto && pair.image && !this.viewerSwapPending && startImmediately) {
          this.scheduleViewerPlaybackStart(260);
        }
      });
    },

    scheduleViewerPlaybackStart(delay = 180, allowVideoOnly = false) {
      if (!this.viewer.pair?.livephoto || (!allowVideoOnly && !this.viewer.pair?.image) || this.playbackMode === "pause") {
        return;
      }
      if (this.viewerPlaybackTimer) {
        window.clearTimeout(this.viewerPlaybackTimer);
      }
      if (this.viewerDeferredTimer) {
        window.clearTimeout(this.viewerDeferredTimer);
        this.viewerDeferredTimer = null;
      }
      const folderName = this.viewer.folder?.folder_name;
      const pairIndex = this.viewer.pair?.pair_index;
      const viewerToken = this.viewerToken;
      this.viewerPlaybackTimer = window.setTimeout(() => {
        this.viewerPlaybackTimer = null;
        if (!this.viewer.open) {
          return;
        }
        if (this.viewerToken !== viewerToken || this.viewer.folder?.folder_name !== folderName || this.viewer.pair?.pair_index !== pairIndex) {
          return;
        }
        const video = document.querySelector(".viewer-video");
        if (!video || video.dataset.viewerToken !== String(viewerToken)) {
          return;
        }
        this.viewer.showVideo = true;
        this.$nextTick(() => this.applyPlaybackMode(video, true));
      }, delay);
    },

    openGalleryPair(item) {
      const sequence = this.buildGalleryViewerSequence();
      const itemKey = item.item_key || `${item.folder_name}::${item.pair_index}`;
      const index = sequence.findIndex((entry) => this.viewerEntryKey(entry) === itemKey);
      this.viewerSource = "gallery";
      if (index >= 0) {
        this.viewerSequence = sequence;
        this.viewerIndex = index;
        this.openViewerEntry(sequence[index]);
        return;
      }
      this.viewerSequence = [
        {
          pair: {
            item_key: itemKey,
            pair_index: item.pair_index,
            image: item.image,
            livephoto: item.livephoto,
            preview_url: item.preview_url,
            preview_kind: item.preview_kind,
            complete: !!(item.image && item.livephoto),
          },
          folder: {
            folder_name: item.folder_name,
            title: item.title,
            pub_time: item.pub_time,
          },
        },
      ];
      this.viewerIndex = 0;
      this.openViewerEntry(this.viewerSequence[0]);
    },

    buildDetailViewerSequence() {
      return (this.detail.pairs || []).map((pair) => ({
        pair: {
          ...pair,
          item_key: `${this.detail.folder?.folder_name || ""}::${pair.pair_index}`,
        },
        folder: this.detail.folder,
      }));
    },

    buildGalleryViewerSequence() {
      return (this.gallery.items || [])
        .filter((galleryItem) => !galleryItem.__deleted_placeholder && (galleryItem.image || galleryItem.livephoto))
        .map((galleryItem) => ({
          pair: {
            item_key: galleryItem.item_key || `${galleryItem.folder_name}::${galleryItem.pair_index}`,
            pair_index: galleryItem.pair_index,
            image: galleryItem.image,
            livephoto: galleryItem.livephoto,
            preview_url: galleryItem.preview_url,
            preview_kind: galleryItem.preview_kind,
            complete: !!(galleryItem.image && galleryItem.livephoto),
          },
          folder: {
            folder_name: galleryItem.folder_name,
            title: galleryItem.title,
            pub_time: galleryItem.pub_time,
          },
        }));
    },

    viewerEntryKey(entry) {
      if (!entry) {
        return "";
      }
      return (
        entry.pair?.item_key ||
        `${entry.folder?.folder_name || this.viewer.folder?.folder_name || ""}::${entry.pair?.pair_index || 0}`
      );
    },

    currentViewerEntryKey() {
      return (
        this.viewer.pair?.item_key ||
        `${this.viewer.folder?.folder_name || ""}::${this.viewer.pair?.pair_index || 0}`
      );
    },

    resolvedViewerSequence() {
      return this.viewerSequence || [];
    },

    openViewerFolder() {
      const folderName = this.viewer.folder?.folder_name;
      if (!folderName) {
        return;
      }
      this.closeViewer();
      window.requestAnimationFrame(() => {
        this.openFolder(folderName);
      });
    },

    closeDetail(skipAnimation = false) {
      detailFetchController?.abort();
      detailFetchController = null;
      if (this.viewer.open) {
        this.closeViewer(skipAnimation);
      }
      if (!this.detail.open && !this.detailClosing) {
        return;
      }
      if (this.detailCloseTimer) {
        window.clearTimeout(this.detailCloseTimer);
        this.detailCloseTimer = null;
      }
      this.detailRequestId += 1;
      this.detailLoading = false;
      this.detailClosing = !skipAnimation;
      this.detail = { ...this.detail, open: false };
      const finalizeClose = () => {
        document.querySelectorAll(".detail-thumb img").forEach((image) => image.removeAttribute("src"));
        document.querySelectorAll(".detail-thumb video, .detail-video-grid video").forEach((video) => {
          video.pause();
          video.removeAttribute("src");
          video.load();
        });
        this.detailClosing = false;
        this.detail = { open: false, pairs: [], folder: null, videos: [] };
        this.detailTextExpanded = false;
        this.detailTextOverflowing = false;
        this.resetDetailWindow();
        this.detailCloseTimer = null;
        this.syncBodyLock();
      };
      if (skipAnimation) {
        finalizeClose();
        return;
      }
      this.syncBodyLock();
      this.detailCloseTimer = window.setTimeout(finalizeClose, 260);
    },

    closeViewer(skipAnimation = false) {
      this.viewerHandoff = null;
      this.cancelViewerSwipe();
      this.cancelViewerEdgeBounce();
      this.cancelViewerOriginal();
      this.viewerImageLoading = false;
      this.hideViewerPositionHint();
      if (this.viewerToolbarHideTimer) window.clearTimeout(this.viewerToolbarHideTimer);
      this.viewerToolbarHideTimer = null;
      this.viewerToolbarOpen = false;
      this.cancelViewerPreviewRetirement();
      if (!this.viewer.open && !this.viewerClosing) {
        return;
      }
      if (this.viewerCloseTimer) {
        window.clearTimeout(this.viewerCloseTimer);
        this.viewerCloseTimer = null;
      }
      this.pauseCurrentViewerMedia(false);
      this.cancelDeleteViewerPair();
      this.viewerSwapPending = false;
      this.viewerPendingDirection = "";
      this.viewerNavigationLockedUntil = 0;
      this.viewerClosing = !skipAnimation;
      this.viewer = { ...this.viewer, open: false };
      const finalizeClose = () => {
        this.viewerImageReady = false;
        this.pauseCurrentViewerMedia(true);
        this.resetViewerTransform();
        this.viewer = { open: false, pair: null, folder: null, showVideo: false };
        this.viewerSource = "detail";
        this.viewerSequence = [];
        this.viewerIndex = 0;
        this.viewerNavigationLockedUntil = 0;
        this.viewerClosing = false;
        this.viewerCloseTimer = null;
        this.syncBodyLock();
      };
      if (skipAnimation) {
        finalizeClose();
        return;
      }
      this.syncBodyLock();
      this.viewerCloseTimer = window.setTimeout(finalizeClose, 240);
    },

    syncBodyLock() {
      const locked = this.videoNote.open || this.videoNoteClosing || this.detail.open || this.viewer.open || this.detailClosing || this.viewerClosing || this.taskInspector.open || this.sourcePreview.open;
      const html = document.documentElement;
      const body = document.body;
      body.classList.toggle("detail-layer-open", this.detail.open || this.detailClosing || this.taskInspector.open || this.sourcePreview.open);
      body.classList.toggle("viewer-layer-open", this.videoNote.open || this.videoNoteClosing || this.viewer.open || this.viewerClosing);
      body.classList.toggle("overlay-locked", locked);
      if (locked) {
        if (this.bodyLockTop !== null) {
          return;
        }
        const scrollTop = window.scrollY || window.pageYOffset || 0;
        const layoutWidth = body.getBoundingClientRect().width;
        this.bodyLockTop = scrollTop;
        this.bodyLockPaddingRight = body.style.paddingRight || "";
        this.bodyLockStyles = {
          htmlOverflow: html.style.overflow || "",
          htmlScrollbarGutter: html.style.scrollbarGutter || "",
          bodyOverflow: body.style.overflow || "",
          bodyScrollbarGutter: body.style.scrollbarGutter || "",
          bodyPosition: body.style.position || "",
          bodyTop: body.style.top || "",
          bodyLeft: body.style.left || "",
          bodyRight: body.style.right || "",
          bodyWidth: body.style.width || "",
          bodyPaddingRight: body.style.paddingRight || "",
        };
        html.style.overflow = "hidden";
        html.style.scrollbarGutter = "auto";
        body.style.scrollbarGutter = "auto";
        body.style.overflow = "hidden";
        body.style.position = "fixed";
        body.style.top = `-${scrollTop}px`;
        body.style.left = "0";
        body.style.right = "auto";
        body.style.width = `${layoutWidth}px`;
        return;
      }
      if (this.bodyLockTop === null) {
        return;
      }
      const scrollTop = this.bodyLockTop;
      const previousStyles = this.bodyLockStyles || {};
      html.classList.add("scroll-restore-instant");
      html.style.overflow = previousStyles.htmlOverflow || "";
      html.style.scrollbarGutter = previousStyles.htmlScrollbarGutter || "";
      body.style.overflow = previousStyles.bodyOverflow || "";
      body.style.scrollbarGutter = previousStyles.bodyScrollbarGutter || "";
      body.style.position = previousStyles.bodyPosition || "";
      body.style.top = previousStyles.bodyTop || "";
      body.style.left = previousStyles.bodyLeft || "";
      body.style.right = previousStyles.bodyRight || "";
      body.style.width = previousStyles.bodyWidth || "";
      body.style.paddingRight = previousStyles.bodyPaddingRight ?? (this.bodyLockPaddingRight || "");
      this.bodyLockTop = null;
      this.bodyLockPaddingRight = "";
      this.bodyLockStyles = null;
      if (scrollTop !== null) {
        window.scrollTo(0, scrollTop);
        document.documentElement.scrollTop = scrollTop;
        document.body.scrollTop = scrollTop;
      }
      const removeInstantRestore = () => html.classList.remove("scroll-restore-instant");
      if (typeof window.requestAnimationFrame === "function") {
        window.requestAnimationFrame(removeInstantRestore);
      } else {
        window.setTimeout(removeInstantRestore, 0);
      }
    },

    cloneDetailPayload(payload) {
      return {
        open: false,
        pairs: (payload?.pairs || []).map((pair) => ({ ...pair })),
        folder: payload?.folder ? { ...payload.folder } : null,
        videos: (payload?.videos || []).map((video) => ({ ...video })),
      };
    },

    cachedDetail(folderName) {
      const cached = this.detailCache[folderName];
      if (!cached) {
        return null;
      }
      this.touchDetailCache(folderName);
      return this.cloneDetailPayload(cached);
    },

    cacheDetailPayload(folderName, payload) {
      if (!folderName || !payload?.folder) {
        return;
      }
      const nextCache = {
        ...this.detailCache,
        [folderName]: this.cloneDetailPayload(payload),
      };
      const nextOrder = [...(this.detailCacheOrder || []).filter((key) => key !== folderName), folderName];
      while (nextOrder.length > this.detailCacheLimit) {
        delete nextCache[nextOrder.shift()];
      }
      this.detailCache = nextCache;
      this.detailCacheOrder = nextOrder;
    },

    touchDetailCache(folderName) {
      if (!folderName || !this.detailCache[folderName]) {
        return;
      }
      const nextOrder = [...(this.detailCacheOrder || []).filter((key) => key !== folderName), folderName];
      const nextCache = { ...this.detailCache };
      while (nextOrder.length > this.detailCacheLimit) {
        delete nextCache[nextOrder.shift()];
      }
      this.detailCache = nextCache;
      this.detailCacheOrder = nextOrder;
    },

    invalidateDetailCache(folderName) {
      if (!folderName || !this.detailCache[folderName]) {
        return;
      }
      const nextCache = { ...this.detailCache };
      delete nextCache[folderName];
      this.detailCache = nextCache;
      this.detailCacheOrder = (this.detailCacheOrder || []).filter((key) => key !== folderName);
    },

    updateCachedDetailFavorite(folderName, favorite) {
      const cached = this.detailCache[folderName];
      if (!cached?.folder) {
        return;
      }
      this.detailCache = {
        ...this.detailCache,
        [folderName]: {
          ...cached,
          folder: { ...cached.folder, is_favorite: favorite },
        },
      };
    },

    removeCachedDetailPair(folderName, pairIndex) {
      const cached = this.detailCache[folderName];
      if (!cached) {
        return;
      }
      const pairs = (cached.pairs || []).filter((pair) => Number(pair.pair_index) !== Number(pairIndex));
      const removedPair = (cached.pairs || []).find((pair) => Number(pair.pair_index) === Number(pairIndex));
      const delta = this.pairAssetDelta(removedPair);
      const folder = cached.folder
        ? {
            ...cached.folder,
            image_count: Math.max(0, (cached.folder.image_count || 0) - delta.images),
            livephoto_count: Math.max(0, (cached.folder.livephoto_count || 0) - delta.livephotos),
            asset_count: Math.max(0, (cached.folder.asset_count || 0) - delta.total),
          }
        : cached.folder;
      this.detailCache = {
        ...this.detailCache,
        [folderName]: { ...cached, pairs, folder },
      };
    },

    hoverPreview(video, shouldPlay) {
      if (!video) return;
      if (shouldPlay) {
        video.play().catch(() => {});
        return;
      }
      video.pause();
      video.currentTime = 0;
    },

    startHoverPreview(event) {
      if (!this.hoverPreviewEnabled) {
        return;
      }
      if (this.hoverPreviewTimer) {
        window.clearTimeout(this.hoverPreviewTimer);
        this.hoverPreviewTimer = null;
      }
      const card = event.currentTarget;
      this.hoverPreviewCard = card;
      this.hoverPreviewTimer = window.setTimeout(() => {
        if (this.hoverPreviewCard === card) {
          this.loadHoverPreview(card);
        }
      }, 380);
    },

    loadHoverPreview(card) {
      const video = card.querySelector(".hover-preview-video");
      const image = card.querySelector(".hover-preview-image");
      const src = video?.dataset?.src || "";
      if (!video || !src) {
        return;
      }
      if (this.hoverPreviewLoadCard && this.hoverPreviewLoadCard !== card) {
        this.stopHoverPreviewByCard(this.hoverPreviewLoadCard);
      }
      this.hoverPreviewLoadCard = card;
      video.muted = true;
      video.playsInline = true;
      video.loop = false;
      video.preload = "auto";
      if (!video.dataset.loaded || video.src !== src) {
        video.src = src;
        video.dataset.loaded = "1";
        video.load();
      }
      if (image) {
        image.classList.add("hidden");
      }
      video.classList.remove("hidden");
      try {
        video.currentTime = 0;
      } catch (_error) {
        // Some browsers reject currentTime before metadata is available.
      }
      video.onended = () => this.stopHoverPreviewByCard(card);
      video.onerror = () => this.stopHoverPreviewByCard(card);
      video.onpause = () => {
        if (video.ended && this.hoverPreviewLoadCard === card) {
          this.stopHoverPreviewByCard(card);
        }
      };
      const play = () => {
        if (this.hoverPreviewLoadCard !== card) {
          return;
        }
        video.currentTime = 0;
        video.play().catch(() => this.stopHoverPreviewByCard(card));
      };
      if (video.readyState >= 2) {
        play();
      } else {
        video.onloadeddata = play;
      }
    },

    stopHoverPreview(event) {
      if (this.hoverPreviewTimer) {
        window.clearTimeout(this.hoverPreviewTimer);
        this.hoverPreviewTimer = null;
      }
      this.hoverPreviewCard = null;
      this.stopHoverPreviewByCard(event.currentTarget);
    },

    scheduleHoverPreview(event) {
      if (!this.hoverPreviewEnabled || this.compactViewport) {
        return;
      }
      const card = event.currentTarget;
      if (this.hoverPreviewTimer) {
        window.clearTimeout(this.hoverPreviewTimer);
      }
      this.hoverPreviewCard = card;
      this.hoverPreviewTimer = window.setTimeout(() => {
        if (this.hoverPreviewCard === card) {
          this.loadHoverPreview(card);
        }
      }, 420);
    },

    cancelScheduledHoverPreview(event) {
      const card = event.currentTarget;
      if (this.hoverPreviewTimer && this.hoverPreviewCard === card) {
        window.clearTimeout(this.hoverPreviewTimer);
        this.hoverPreviewTimer = null;
      }
      this.hoverPreviewCard = null;
      this.stopHoverPreviewByCard(card);
    },

    stopHoverPreviewByCard(card) {
      const video = card.querySelector(".hover-preview-video");
      const image = card.querySelector(".hover-preview-image");
      const wasActive = this.hoverPreviewLoadCard === card;
      if (wasActive) {
        this.hoverPreviewLoadCard = null;
      }
      if (video) {
        video.onloadeddata = null;
        video.onended = null;
        video.onerror = null;
        video.onpause = null;
        video.pause();
        try {
          video.currentTime = 0;
        } catch (_error) {
          // Metadata may not have loaded yet.
        }
        video.removeAttribute("src");
        video.removeAttribute("data-loaded");
        video.load();
        video.classList.add("hidden");
      }
      if (image) {
        image.classList.remove("hidden");
      }
    },

    playbackModeLabel() {
      return {
        loop: "循环",
        pingpong: "乒乓",
        once: "仅播放一次",
        pause: "不播放",
      }[this.playbackMode] || "循环";
    },

    availableMonths() {
      const months = [];
      Object.values(this.meta.years || {}).forEach((group) => {
        group.forEach((month) => months.push(month));
      });
      return months;
    },

    currentMonthLabel(month) {
      if (!month) return "全部时间";
      const [year, monthValue] = month.split("-");
      return `${year}年${Number(monthValue)}月`;
    },

    timeRangeLabel() {
      const months = this.availableMonths();
      if (!months.length) {
        return "全部时间";
      }
      if (this.timeSortMode === "random") {
        return "随机排序";
      }
      const startMonth = this.timeFilterApplied.startMonth;
      const endMonth = this.timeFilterApplied.endMonth;
      if (!startMonth || !endMonth) {
        return "全部时间";
      }
      if (startMonth === endMonth) {
        return this.currentMonthLabel(startMonth);
      }
      return `${this.currentMonthLabel(startMonth)} 至 ${this.currentMonthLabel(endMonth)}`;
    },

    timeFilterActive() {
      return !!(this.timeFilterApplied.startMonth || this.timeFilterApplied.endMonth);
    },

    timeSortLabel() {
      return {
        desc: "最新发布",
        asc: "最早发布",
        random: "随机排序",
        imported: "最近入库",
      }[this.timeSortMode] || "最近入库";
    },

    toggleTimeFilter() {
      this.timeFilterOpen = !this.timeFilterOpen;
      if (this.timeFilterOpen) {
        this.syncTimeFilterDraft();
      }
    },

    syncTimeFilterDraft() {
      const months = this.availableMonths();
      if (!months.length) {
        this.timeFilterDraft = { startIndex: 0, endIndex: 0 };
        return;
      }
      const startMonth = this.timeFilterApplied.startMonth || months[0];
      const endMonth = this.timeFilterApplied.endMonth || months[months.length - 1];
      const startIndex = Math.max(months.indexOf(startMonth), 0);
      const endIndex = Math.max(months.indexOf(endMonth), startIndex);
      this.timeFilterDraft = { startIndex, endIndex };
    },

    timeFilterHandleStyle(handle) {
      const months = this.availableMonths();
      const total = Math.max(months.length - 1, 1);
      const index = handle === "start" ? this.timeFilterDraft.startIndex : this.timeFilterDraft.endIndex;
      const progress = total === 0 ? 0 : index / total;
      return `left: ${Math.round(progress * 1000) / 10}%;`;
    },

    timeFilterRangeStyle() {
      const months = this.availableMonths();
      const total = Math.max(months.length - 1, 1);
      const start = total === 0 ? 0 : this.timeFilterDraft.startIndex / total;
      const end = total === 0 ? 0 : this.timeFilterDraft.endIndex / total;
      return `left: ${Math.round(start * 1000) / 10}%; width: ${Math.max((end - start) * 100, 2)}%;`;
    },

    timeFilterSummary() {
      const months = this.availableMonths();
      if (!months.length) {
        return "暂无可筛选时间";
      }
      const startMonth = months[this.timeFilterDraft.startIndex];
      const endMonth = months[this.timeFilterDraft.endIndex];
      if (!startMonth || !endMonth) {
        return "全部时间";
      }
      if (startMonth === endMonth) {
        return this.currentMonthLabel(startMonth);
      }
      return `${this.currentMonthLabel(startMonth)} 至 ${this.currentMonthLabel(endMonth)}`;
    },

    timeFilterYearMarks() {
      const months = this.availableMonths();
      const total = Math.max(months.length - 1, 1);
      const entries = Object.keys(this.meta.years || {}).map((year) => {
        const firstMonth = (this.meta.years[year] || [])[0];
        const index = Math.max(months.indexOf(firstMonth), 0);
        return {
          year,
          progress: index / total,
        };
      });
      const filtered = [];
      let lastProgress = -1;
      entries.forEach((entry, index) => {
        const isEdge = index === 0 || index === entries.length - 1;
        if (!isEdge && entry.progress - lastProgress < 0.12) {
          return;
        }
        lastProgress = entry.progress;
        filtered.push({
          year: entry.year,
          style: `left: ${Math.round(entry.progress * 1000) / 10}%;`,
        });
      });
      return filtered;
    },

    timeFilterMonthTicks() {
      const months = this.availableMonths();
      const total = Math.max(months.length - 1, 1);
      return months.map((month, index) => ({
        month,
        style: `left: ${Math.round((index / total) * 1000) / 10}%;`,
      }));
    },

    timeFilterPointerDown(event, handle) {
      event.preventDefault();
      this.timeFilterDrag = { active: true, handle };
      this.updateTimeFilterFromPoint(event.clientX);
    },

    timeFilterPointerMove(event) {
      if (!this.timeFilterDrag.active) {
        return;
      }
      this.updateTimeFilterFromPoint(event.clientX);
    },

    finishTimeFilterDrag() {
      if (!this.timeFilterDrag.active) {
        return;
      }
      this.timeFilterDrag = { active: false, handle: null };
      this.commitTimeFilter();
    },

    updateTimeFilterFromPoint(clientX) {
      const zone = document.querySelector(".time-filter-track");
      const months = this.availableMonths();
      if (!zone || !months.length) {
        return;
      }
      const rect = zone.getBoundingClientRect();
      const relative = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(rect.width, 1)));
      const index = Math.round(relative * Math.max(months.length - 1, 1));
      if (this.timeFilterDrag.handle === "start") {
        this.timeFilterDraft.startIndex = Math.min(index, this.timeFilterDraft.endIndex);
        return;
      }
      this.timeFilterDraft.endIndex = Math.max(index, this.timeFilterDraft.startIndex);
    },

    timeFilterWheel(event) {
      const months = this.availableMonths();
      if (months.length <= 1) {
        return;
      }
      event.preventDefault();
      const step = event.deltaY > 0 ? 1 : -1;
      if (event.shiftKey) {
        this.timeFilterDraft.endIndex = Math.max(
          this.timeFilterDraft.startIndex,
          Math.min(months.length - 1, this.timeFilterDraft.endIndex + step),
        );
      } else {
        const span = this.timeFilterDraft.endIndex - this.timeFilterDraft.startIndex;
        let nextStart = Math.max(0, Math.min(months.length - 1 - span, this.timeFilterDraft.startIndex + step));
        let nextEnd = nextStart + span;
        if (nextEnd > months.length - 1) {
          nextEnd = months.length - 1;
          nextStart = Math.max(0, nextEnd - span);
        }
        this.timeFilterDraft.startIndex = nextStart;
        this.timeFilterDraft.endIndex = nextEnd;
      }
      this.scheduleTimeFilterCommit();
    },

    scheduleTimeFilterCommit() {
      if (this.timeFilterApplyTimer) {
        window.clearTimeout(this.timeFilterApplyTimer);
      }
      this.timeFilterApplyTimer = window.setTimeout(() => {
        this.commitTimeFilter();
      }, 140);
    },

    async commitTimeFilter() {
      if (this.timeFilterApplyTimer) {
        window.clearTimeout(this.timeFilterApplyTimer);
        this.timeFilterApplyTimer = null;
      }
      const months = this.availableMonths();
      if (!months.length) {
        return;
      }
      const startMonth = months[this.timeFilterDraft.startIndex] || null;
      const endMonth = months[this.timeFilterDraft.endIndex] || null;
      const applyingFullRange = this.timeFilterDraft.startIndex === 0 && this.timeFilterDraft.endIndex === months.length - 1;
      const currentStart = this.timeFilterApplied.startMonth || months[0];
      const currentEnd = this.timeFilterApplied.endMonth || months[months.length - 1];
      if (startMonth === currentStart && endMonth === currentEnd) {
        return;
      }
      this.timeFilterApplied = applyingFullRange
        ? { startMonth: null, endMonth: null }
        : { startMonth, endMonth };
      this.scrollViewTop();
      this.refreshGalleryImmediately(true);
    },

    clearTimeFilter() {
      if (!this.timeFilterApplied.startMonth && !this.timeFilterApplied.endMonth) {
        return;
      }
      const months = this.availableMonths();
      this.timeFilterApplied = { startMonth: null, endMonth: null };
      this.timeFilterDraft = { startIndex: 0, endIndex: Math.max(months.length - 1, 0) };
      this.scrollViewTop();
      this.refreshGalleryImmediately(true);
    },

    reviewSourceUrl(item) {
      if (item?.original_url) {
        return item.original_url;
      }
      const dynamicId = item?.source_dynamic_id || item?.top_dynamic_id;
      if (dynamicId && String(dynamicId).startsWith("site:")) {
        return "";
      }
      return dynamicId ? `https://www.bilibili.com/opus/${dynamicId}` : "";
    },

    openReviewSource(item) {
      const url = this.reviewSourceUrl(item);
      if (!url) {
        this.notify("error", "无法打开原始动态", "当前待审核项没有可用的动态编号。");
        return;
      }
      const xhsNote = /^xhs_/.test(item?.folder_name || '') || /^https:\/\/www\.xiaohongshu\.com\/(?:explore|discovery\/item)\//.test(url);
      if (this.settings?.review_source_open_mode === "popup" && !xhsNote) {
        const reviewItem = this.currentView === "review"
          ? (this.reviewItems || []).find((entry) => Number(entry.id) === Number(item?.id))
          : null;
        this.sourcePreview = {
          open: true,
          url,
          title: item?.folder_name_candidate || item?.title || "原始内容",
          subtitle: url,
          reviewId: reviewItem ? Number(reviewItem.id) : null,
        };
        this.syncBodyLock();
        return;
      }
      this.openExternalUrl(url);
    },

    openExternalUrl(url) {
      window.open(url, "_blank", "noopener,noreferrer");
    },

    openSourcePreviewExternal() {
      if (this.sourcePreview.url) {
        this.openExternalUrl(this.sourcePreview.url);
      }
    },

    closeSourcePreview() {
      if (!this.sourcePreview.open) {
        return;
      }
      this.sourcePreview = { open: false, url: "", title: "", subtitle: "", reviewId: null };
      this.syncBodyLock();
    },

    resolveSourcePreviewReview(action) {
      const reviewId = Number(this.sourcePreview.reviewId);
      if (!reviewId || this.reviewActionId === reviewId) {
        return;
      }
      this.closeSourcePreview();
      if (action === "reject") {
        this.rejectReview(reviewId);
        return;
      }
      this.approveReview(reviewId);
    },

    openTrashSource(item) {
      this.openReviewSource(item.folder || item);
    },

    cyclePlayback() {
      const index = this.playbackModes.indexOf(this.playbackMode);
      this.playbackMode = this.playbackModes[(index + 1) % this.playbackModes.length];
      this.scheduleIdleWork(() => localStorage.setItem("livephoto_playback_mode", this.playbackMode), 500);
      document.querySelectorAll(".viewer-video").forEach((video) => this.applyPlaybackMode(video, true));
    },

    toggleViewerPlayback() {
      if (!this.viewer.pair?.livephoto) {
        return;
      }
      if (this.viewer.showVideo) {
        this.pauseCurrentViewerMedia(true);
        if (this.viewer.pair?.image) {
          this.viewer.showVideo = false;
        }
        return;
      }
      const video = document.querySelector(".viewer-video");
      if (!video || video.dataset.viewerToken !== String(this.viewerToken)) {
        return;
      }
      this.viewer.showVideo = true;
      this.$nextTick(() => this.applyPlaybackMode(video, true));
    },

    viewerVisibleMedia() {
      return document.querySelector(".viewer-stage-image:not(.hidden), .viewer-stage-preview:not(.hidden), .viewer-video:not(.hidden)");
    },

    pointInsideRect(clientX, clientY, rect) {
      return clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom;
    },

    viewerMediaDisplayRect(stage, media) {
      const stageRect = stage?.getBoundingClientRect?.() || media.getBoundingClientRect();
      const naturalWidth = media.tagName === "VIDEO" ? (media.videoWidth || media.clientWidth) : (media.naturalWidth || media.clientWidth);
      const naturalHeight = media.tagName === "VIDEO" ? (media.videoHeight || media.clientHeight) : (media.naturalHeight || media.clientHeight);
      if (!naturalWidth || !naturalHeight) {
        return media.getBoundingClientRect();
      }
      const scale = Math.min(stageRect.width / naturalWidth, stageRect.height / naturalHeight);
      const displayWidth = naturalWidth * scale;
      const displayHeight = naturalHeight * scale;
      const left = stageRect.left + (stageRect.width - displayWidth) / 2;
      const top = stageRect.top + (stageRect.height - displayHeight) / 2;
      return {
        left,
        top,
        right: left + displayWidth,
        bottom: top + displayHeight,
        width: displayWidth,
        height: displayHeight,
      };
    },

    handleViewerTap(clientX, clientY, stage = null) {
      if (this.viewerZoom > 1.02) {
        return;
      }
      const media = this.viewerVisibleMedia();
      if (media) {
        const mediaRect = this.viewerMediaDisplayRect(stage, media);
        if (this.pointInsideRect(clientX, clientY, mediaRect)) {
          if (this.viewer.pair?.livephoto) {
            this.toggleViewerPlayback();
          }
          return;
        }
      }
      if (stage) {
        const stageRect = stage.getBoundingClientRect();
        if (this.pointInsideRect(clientX, clientY, stageRect)) {
          this.closeViewer();
          return;
        }
      }
      this.closeViewer();
    },

    queueViewerTap(clientX, clientY, stage) {
      if (this.viewerClickTimer) {
        window.clearTimeout(this.viewerClickTimer);
      }
      this.viewerClickTimer = window.setTimeout(() => {
        this.viewerClickTimer = null;
        this.handleViewerTap(clientX, clientY, stage);
      }, 220);
    },

    applyPlaybackMode(video, restart = false) {
      const forward = video.dataset.forward;
      const reverse = video.dataset.reverse;
      const hasImage = video.dataset.hasImage === "1";
      const viewerToken = String(this.viewerToken);
      const currentForward = this.viewer.pair?.livephoto?.url || "";
      if (!forward || video.dataset.viewerToken !== viewerToken || forward !== currentForward) return;
      video.loop = false;
      video.onended = null;
      if (this.playbackMode === "pause") {
        this.viewer.showVideo = false;
        video.pause();
        video.removeAttribute("src");
        video.load();
        video.setAttribute("poster", video.getAttribute("poster") || "");
        return;
      }
      this.viewer.showVideo = true;
      if (!video.src || restart) {
        video.src = forward;
      }
      if (this.playbackMode === "loop") {
        video.loop = true;
        if (restart) video.currentTime = 0;
        video.play().catch(() => {});
        return;
      }
      if (this.playbackMode === "once") {
        video.onended = () => {
          if (video.dataset.viewerToken !== String(this.viewerToken)) {
            return;
          }
          if (hasImage) {
            this.viewer.showVideo = false;
          }
        };
        if (restart) video.currentTime = 0;
        video.play().catch(() => {});
        return;
      }
      if (this.playbackMode === "pingpong") {
        if (!reverse) {
          video.loop = true;
          video.play().catch(() => {});
          return;
        }
        video.onended = () => {
          if (video.dataset.viewerToken !== String(this.viewerToken)) {
            return;
          }
          video.src = video.src.endsWith(reverse) ? forward : reverse;
          video.load();
          video.play().catch(() => {});
        };
        if (restart) {
          video.src = forward;
          video.load();
        }
        video.play().catch(() => {});
      }
    },

    replayViewerOnce() {
      if (this.playbackMode !== "once" || !this.viewer.pair?.livephoto || this.viewer.showVideo) {
        return;
      }
      const video = document.querySelector(".viewer-video");
      if (!video || video.dataset.viewerToken !== String(this.viewerToken)) {
        return;
      }
      this.viewer.showVideo = true;
      this.$nextTick(() => this.applyPlaybackMode(video, true));
    },

    currentViewerSequenceIndex(sequence = this.resolvedViewerSequence()) {
      const currentKey = this.currentViewerEntryKey();
      const currentIndex = (sequence || []).findIndex((entry) => this.viewerEntryKey(entry) === currentKey);
      if (currentIndex >= 0) {
        return currentIndex;
      }
      const fallbackIndex = Number(this.viewerIndex);
      return Number.isInteger(fallbackIndex) && fallbackIndex >= 0 && fallbackIndex < (sequence || []).length
        ? fallbackIndex
        : -1;
    },

    canShowPreviousPair() {
      return !this.viewerSwapPending && this.currentViewerSequenceIndex() > 0;
    },

    canShowNextPair() {
      const sequence = this.resolvedViewerSequence();
      const currentIndex = this.currentViewerSequenceIndex(sequence);
      return !this.viewerSwapPending && currentIndex >= 0 && currentIndex < sequence.length - 1;
    },

    showPreviousPair(fromSwipe = false) {
      if (this.viewerSwapPending || (!fromSwipe && Date.now() < this.viewerNavigationLockedUntil)) {
        return;
      }
      const sequence = this.resolvedViewerSequence();
      const currentIndex = this.currentViewerSequenceIndex(sequence);
      if (currentIndex <= 0) {
        return;
      }
      this.pauseCurrentViewerMedia(true);
      this.viewerSequence = sequence;
      this.viewerIndex = currentIndex - 1;
      this.prepareViewerHandoff(sequence[this.viewerIndex]);
      this.openViewerEntry(sequence[this.viewerIndex], true, false, fromSwipe ? "" : "down");
    },

    showNextPair(fromSwipe = false) {
      if (this.viewerSwapPending || (!fromSwipe && Date.now() < this.viewerNavigationLockedUntil)) {
        return;
      }
      const sequence = this.resolvedViewerSequence();
      const currentIndex = this.currentViewerSequenceIndex(sequence);
      if (currentIndex < 0 || currentIndex >= sequence.length - 1) {
        return;
      }
      this.pauseCurrentViewerMedia(true);
      this.viewerSequence = sequence;
      this.viewerIndex = currentIndex + 1;
      this.prepareViewerHandoff(sequence[this.viewerIndex]);
      this.openViewerEntry(sequence[this.viewerIndex], true, false, fromSwipe ? "" : "up");
    },

    pauseCurrentViewerMedia(resetSource = false) {
      this.viewer.showVideo = false;
      if (this.viewerPlaybackTimer) {
        window.clearTimeout(this.viewerPlaybackTimer);
        this.viewerPlaybackTimer = null;
      }
      if (this.viewerDeferredTimer) {
        window.clearTimeout(this.viewerDeferredTimer);
        this.viewerDeferredTimer = null;
      }
      if (this.viewerClickTimer) {
        window.clearTimeout(this.viewerClickTimer);
        this.viewerClickTimer = null;
      }
      document.querySelectorAll(".viewer-video").forEach((video) => {
        video.pause();
        video.currentTime = 0;
        video.onended = null;
        if (resetSource) {
          video.removeAttribute("src");
          video.load();
        }
      });
    },

    animateViewerSwitch(direction) {
      this.viewerSwitchDirection = "";
      if (this.viewerSwitchTimer) {
        window.clearTimeout(this.viewerSwitchTimer);
      }
      const token = this.viewerToken;
      this.$nextTick(() => {
        if (!this.viewer.open || token !== this.viewerToken) return;
        this.viewerSwitchDirection = direction;
        this.viewerSwitchTimer = window.setTimeout(() => {
          this.viewerSwitchDirection = "";
          this.viewerSwitchTimer = null;
        }, 190);
      });
    },

    resetViewerTransform() {
      this.cancelViewerSwipe();
      this.cancelViewerEdgeBounce();
      this.viewerZoom = 1;
      this.viewerOffsetX = 0;
      this.viewerOffsetY = 0;
      this.viewerTransitionEnabled = true;
      if (this.viewerWheelTimer) {
        window.clearTimeout(this.viewerWheelTimer);
        this.viewerWheelTimer = null;
      }
      if (this.viewerImageRevealTimer) {
        window.clearTimeout(this.viewerImageRevealTimer);
        this.viewerImageRevealTimer = null;
      }
      if (this.viewerPlaybackTimer) {
        window.clearTimeout(this.viewerPlaybackTimer);
        this.viewerPlaybackTimer = null;
      }
      if (this.viewerClickTimer) {
        window.clearTimeout(this.viewerClickTimer);
        this.viewerClickTimer = null;
      }
      this.viewerGesture = {
        pointers: {},
        startX: 0,
        startY: 0,
        startOffsetX: 0,
        startOffsetY: 0,
        startZoom: 1,
        startDistance: 0,
        active: false,
        isPinch: false,
        moved: false,
      };
    },

    cancelViewerOriginal() {
      if (this.viewerImageCancel) this.viewerImageCancel();
      this.viewerImageCancel = null;
      if (this.viewerImageObjectUrl) URL.revokeObjectURL(this.viewerImageObjectUrl);
      this.viewerImageObjectUrl = "";
    },

    loadViewerOriginal(url) {
      this.cancelViewerOriginal();
      this.viewerImageSource = "";
      this.viewerImageProgress = null;
      this.viewerImageLoading = !!url;
      if (!url) return;
      const token = this.viewerToken;
      const request = new XMLHttpRequest();
      let cancelled = false;
      this.viewerImageCancel = () => { cancelled = true; request.abort(); };
      const current = () => !cancelled && token === this.viewerToken;
      const fallback = () => {
        if (!current()) return;
        this.viewerImageCancel = null;
        this.viewerImageProgress = null;
        this.viewerImageSource = url;
      };
      request.open("GET", url);
      request.responseType = "blob";
      request.onprogress = event => {
        if (!current()) return;
        this.viewerImageProgress = event.lengthComputable && event.total > 0
          ? Math.min(99, Math.floor(event.loaded / event.total * 100)) : null;
      };
      request.onload = () => {
        if (!current()) return;
        if (request.status < 200 || request.status >= 300) { fallback(); return; }
        this.viewerImageCancel = null;
        this.viewerImageProgress = 100;
        this.viewerImageObjectUrl = URL.createObjectURL(request.response);
        this.viewerImageSource = this.viewerImageObjectUrl;
      };
      request.onerror = fallback;
      request.ontimeout = fallback;
      request.send();
    },

    viewerOriginalProgressLabel() {
      return this.viewerImageProgress === null ? "…" : `${this.viewerImageProgress}%`;
    },

    cancelViewerPreviewRetirement() {
      if (this.viewerPreviewRetireTimer !== null) window.clearTimeout(this.viewerPreviewRetireTimer);
      this.viewerPreviewRetireTimer = null;
    },

    viewerMediaLoaded(kind, event) {
      const target = event?.target;
      if (!this.viewer.open || !target) {
        return;
      }
      if ((kind === "image" || kind === "video") && target.dataset.viewerToken !== String(this.viewerToken)) {
        return;
      }
      if (kind === "image") {
        const imageToken = this.viewerToken;
        const reveal = () => {
          if (!this.viewer.open || imageToken !== this.viewerToken || target.dataset.viewerToken !== String(this.viewerToken)) {
            return;
          }
          this.viewerImageProgress = 100;
          this.viewerImageReady = true;
          this.viewerImageLoading = false;
          this.viewerImageRevealTimer = null;
          this.cancelViewerPreviewRetirement();
          const token = this.viewerToken;
          this.viewerPreviewRetireTimer = window.setTimeout(() => {
            this.viewerPreviewRetireTimer = null;
            if (this.viewer.open && token === this.viewerToken && this.viewerImageReady) {
              this.viewerPreviewRetired = true;
              this.viewerHandoff = null;
            }
          }, window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? 0 : 400);
        };
        const scheduleReveal = () => {
          if (!this.viewer.open || imageToken !== this.viewerToken) return;
          // Establish the transparent original before starting its overlap with the preview.
          target.getBoundingClientRect?.();
          const waitMs = Math.max(0, Number(this.viewerImageRevealAfter || 0) - Date.now());
          if (this.viewerImageRevealTimer) window.clearTimeout(this.viewerImageRevealTimer);
          this.viewerImageRevealTimer = window.setTimeout(reveal, waitMs);
        };
        if (typeof target.decode === "function") {
          target.decode().then(scheduleReveal).catch(() => this.viewerMediaFailed("image", event));
        } else scheduleReveal();
      }
      this.releaseViewerSwap();
      if (kind === "image" && this.viewer.pair?.livephoto && !this.viewer.showVideo && this.playbackMode !== "pause") {
        this.scheduleViewerPlaybackStart();
      }
    },

    viewerOriginalTransitionEnded(event) {
      if (event?.propertyName !== "opacity" || event.target?.dataset.viewerToken !== String(this.viewerToken)
          || !this.viewer.open || !this.viewerImageReady) return;
      this.cancelViewerPreviewRetirement();
      this.viewerPreviewRetired = true;
      this.viewerHandoff = null;
    },

    viewerMediaFailed(kind, event) {
      const target = event?.target;
      if (!this.viewer.open || !target || target.dataset.viewerToken !== String(this.viewerToken)) {
        return;
      }
      if (kind === "image") {
        this.viewerImageReady = false;
        this.viewerImageLoading = false;
        if (this.viewerImageRevealTimer) {
          window.clearTimeout(this.viewerImageRevealTimer);
          this.viewerImageRevealTimer = null;
        }
      }
      this.releaseViewerSwap();
    },

    releaseViewerSwap() {
      if (this.viewerSwapPending) {
        const direction = this.viewerPendingDirection;
        this.viewerSwapPending = false;
        this.viewerPendingDirection = "";
        if (direction) {
          this.animateViewerSwitch(direction);
        }
      }
    },

    viewerMediaStyle() {
      return `transform: translate3d(${this.viewerOffsetX}px, ${this.viewerOffsetY}px, 0) scale(${this.viewerZoom});`;
    },

    viewerShouldShowOriginalLoading() {
      return !!(this.viewer.open && this.viewerImageLoading && this.viewer.pair?.image
        && !(this.viewer.showVideo && this.viewer.pair?.livephoto));
    },

    viewerPreviewUrl() {
      // 查看器优先使用单档 thumb，最后显式回退静态原图。
      return (
        pickThumbUrl([this.viewer.pair?.image, this.viewer.pair?.livephoto], THUMB_PROMOTED_LEVELS) ||
        this.viewer.pair?.preview_url ||
        this.viewer.pair?.image?.url ||
        ""
      );
    },

    livephotoViewerPoster() {
      return pickThumbUrl([this.viewer.pair?.livephoto], THUMB_LEVELS);
    },

    viewerStageClick(event) {
      event.stopPropagation();
      if (Date.now() < this.viewerSyntheticTapUntil) {
        return;
      }
      if (this.viewerZoom > 1.02) {
        return;
      }
      this.queueViewerTap(event.clientX, event.clientY, event.currentTarget);
    },

    viewerDoubleClick(event) {
      event.preventDefault();
      event.stopPropagation();
      if (this.viewerClickTimer) {
        window.clearTimeout(this.viewerClickTimer);
        this.viewerClickTimer = null;
      }
      if (this.viewerZoom > 1.02) {
        this.resetViewerTransform();
        return;
      }
      this.setViewerZoomLevel(2, event);
    },

    viewerShellClick(event) {
      if (event.target.closest(".viewer-floating")) {
        return;
      }
      if (event.target.closest(".viewer-media")) {
        return;
      }
      this.closeViewer();
    },

    revealViewerToolbar() {
      this.hideViewerPositionHint();
      if (this.viewerToolbarHideTimer) window.clearTimeout(this.viewerToolbarHideTimer);
      this.viewerToolbarHideTimer = null;
      this.viewerToolbarOpen = true;
    },

    hideViewerPositionHint() {
      if (this.viewerPositionHintTimer !== null) window.clearTimeout(this.viewerPositionHintTimer);
      this.viewerPositionHintTimer = null;
      this.viewerPositionHintVisible = false;
    },

    showViewerPositionHint() {
      this.hideViewerPositionHint();
      if (!this.viewer.open || this.viewerToolbarOpen) return;
      this.viewerPositionHintVisible = true;
      this.viewerPositionHintTimer = window.setTimeout(() => {
        this.viewerPositionHintTimer = null;
        this.viewerPositionHintVisible = false;
      }, 1200);
    },

    scheduleViewerToolbarHide() {
      if (!this.viewerToolbarOpen || this.viewerToolbarHideTimer) return;
      this.viewerToolbarHideTimer = window.setTimeout(() => {
        this.viewerToolbarHideTimer = null;
        const toolbar = document.querySelector(".viewer-floating-right");
        if (!toolbar?.matches(":focus-visible") && !toolbar?.querySelector(":focus-visible")) this.viewerToolbarOpen = false;
      }, 1000);
    },

    viewerPointerNearToolbar(event) {
      if (event.pointerType !== "mouse" || this.viewerGesture.active) return;
      if (event.target.closest?.(".viewer-floating-right") || event.clientX >= window.innerWidth - 88) {
        this.revealViewerToolbar();
      } else this.scheduleViewerToolbarHide();
    },

    prepareViewerHandoff(entry) {
      const pair = entry?.pair;
      const url = this.detailPairPreviewUrl(pair) || pair?.preview_url || pair?.image?.url || "";
      this.viewerHandoff = url ? { key: this.viewerEntryKey(entry), url } : null;
    },

    viewerHandoffStyle() {
      return {
        transform: `translate3d(${this.viewerOffsetX}px, ${this.viewerOffsetY}px, 0) scale(${this.viewerZoom})`,
        backgroundImage: this.viewerHandoff?.url ? `url(${JSON.stringify(this.viewerHandoff.url)})` : "none",
      };
    },

    viewerPreviewLoaded(event) {
      const target = event?.target;
      const token = this.viewerToken;
      const reveal = () => {
        if (!this.viewer.open || token !== this.viewerToken || target?.dataset.viewerToken !== String(token)) return;
        target.dataset.previewReady = "1";
        const release = () => {
          if (this.viewer.open && token === this.viewerToken) this.viewerHandoff = null;
        };
        if (window.requestAnimationFrame) window.requestAnimationFrame(release); else release();
      };
      if (typeof target?.decode === "function") target.decode().then(reveal).catch(() => {});
      else reveal();
    },

    viewerNeighbor(direction) {
      const sequence = this.resolvedViewerSequence();
      return sequence[this.currentViewerSequenceIndex(sequence) + direction]?.pair;
    },

    viewerNeighborUrl(direction) {
      return this.detailPairPreviewUrl(this.viewerNeighbor(direction));
    },

    viewerNeighborStyle(direction) {
      const url = this.viewerNeighborUrl(direction);
      return {
        transform: `translate3d(0, ${this.viewerOffsetY + direction * window.innerHeight}px, 0)`,
        backgroundImage: url ? `url(${JSON.stringify(url)})` : "none",
      };
    },

    cancelViewerSwipe() {
      if (this.viewerSwipeTimer !== null) window.clearTimeout(this.viewerSwipeTimer);
      this.viewerSwipeTimer = null;
      this.viewerSwipeFinish = null;
      if (this.viewerSwipeCancelAnimations) this.viewerSwipeCancelAnimations();
      this.viewerSwipeCancelAnimations = null;
      this.viewerVerticalDragging = false;
    },

    finishViewerVerticalSwipe(direction) {
      const canMove = direction > 0 ? this.canShowNextPair() : this.canShowPreviousPair();
      if (!canMove) { this.springViewerEdge(); return; }
      const token = this.viewerToken;
      this.viewerVerticalDragging = true;
      const height = window.innerHeight;
      const animations = [];
      const commit = () => {
        if (this.viewerSwipeFinish !== commit) return;
        this.cancelViewerSwipe();
        if (!this.viewer.open || token !== this.viewerToken) return;
        this.viewerSwitchDirection = "";
        if (direction > 0) this.showNextPair(true); else this.showPreviousPair(true);
      };
      this.viewerSwipeFinish = commit;
      this.viewerSwipeCancelAnimations = () => animations.forEach(animation => animation.cancel());
      if (!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
        document.querySelectorAll(".viewer-stage-image, .viewer-stage-preview, .viewer-stage-handoff, .viewer-video, .viewer-stage-neighbor").forEach(node => {
          if (!node.animate) return;
          const neighbor = Number(node.dataset.neighbor || 0);
          const start = this.viewerOffsetY + neighbor * height;
          const end = (neighbor - direction) * height;
          animations.push(node.animate([
            { transform: `translate3d(0, ${start}px, 0)` },
            { transform: `translate3d(0, ${end}px, 0)` },
          ], { duration: 120, easing: "cubic-bezier(0.2, 0.7, 0.3, 1)", fill: "forwards" }));
        });
      }
      if (!animations.length) { commit(); return; }
      animations.at(-1).onfinish = commit;
      this.viewerSwipeTimer = window.setTimeout(commit, 170);
    },

    viewerTrackpadOffset(distance) {
      const limit = Math.min(200, window.innerHeight * 0.25);
      return Math.sign(distance) * limit * (1 - Math.exp(-Math.abs(distance) * 0.75 / limit));
    },

    viewerEdgeOffset(distance) {
      return Math.sign(distance) * 36 * (1 - Math.exp(-Math.abs(distance) / 150));
    },

    cancelViewerEdgeBounce() {
      if (this.viewerEdgeWheelTimer !== null) window.clearTimeout(this.viewerEdgeWheelTimer);
      this.viewerEdgeWheelTimer = null;
      if (this.viewerEdgeCancel) this.viewerEdgeCancel();
      this.viewerEdgeCancel = null;
    },

    springViewerEdge() {
      const offset = this.viewerOffsetY;
      this.viewerOffsetX = 0;
      this.viewerOffsetY = 0;
      this.viewerTransitionEnabled = true;
      if (!offset || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
      const animations = Array.from(document.querySelectorAll(
        ".viewer-stage-image, .viewer-stage-preview, .viewer-stage-handoff, .viewer-video",
      )).filter(node => typeof node.animate === "function").map(node => node.animate([
        { transform: `translate3d(0, ${offset}px, 0) scale(1)`, offset: 0 },
        { transform: `translate3d(0, ${-offset * 0.12}px, 0) scale(1)`, offset: 0.65 },
        { transform: "translate3d(0, 0, 0) scale(1)", offset: 1 },
      ], { duration: 460, easing: "cubic-bezier(0.2, 0.7, 0.3, 1)" }));
      const cancel = () => animations.forEach(animation => animation.cancel());
      this.viewerEdgeCancel = cancel;
      if (animations.length) animations.at(-1).onfinish = () => {
        if (this.viewerEdgeCancel === cancel) this.viewerEdgeCancel = null;
      };
      else this.viewerEdgeCancel = null;
    },

    viewerWheel(event) {
      event.preventDefault();
      if (this.viewerGesture.active || this.viewerGesture.isPinch || !this.viewer.open) return;
      if (event.ctrlKey || event.metaKey) {
        const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? window.innerHeight : 1;
        this.setViewerZoomLevel(this.viewerZoom * Math.exp(-event.deltaY * unit * 0.012), event, false);
        return;
      }
      if (this.viewerZoom > 1.02) {
        this.viewerTransitionEnabled = false;
        const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? window.innerHeight : 1;
        this.viewerOffsetX -= (event.deltaX || 0) * unit;
        this.viewerOffsetY -= (event.deltaY || 0) * unit;
        this.clampViewerTransform(event.currentTarget);
        if (this.viewerWheelTimer) window.clearTimeout(this.viewerWheelTimer);
        this.viewerWheelTimer = window.setTimeout(() => {
          this.viewerTransitionEnabled = true;
          this.viewerWheelTimer = null;
        }, 120);
        return;
      }
      const previous = this.trackpadGesture;
      if (previous?.axis === "y" && previous.fired && Date.now() - previous.lastAt > 110) {
        if (this.viewerSwipeFinish) this.viewerSwipeFinish();
        this.trackpadGesture = null;
      }
      const gesture = this.wheelGestureStep(event, "trackpadGesture", "viewer");
      if (gesture.fired) return;
      if (gesture.axis === "x") {
        this.updateTrackpadBackHint(-gesture.x / 82, "viewer");
      }
      if (gesture.axis === "y") {
        this.cancelViewerEdgeBounce();
        this.viewerTransitionEnabled = false;
        this.viewerVerticalDragging = true;
        const canMove = gesture.y < 0 ? this.canShowPreviousPair() : this.canShowNextPair();
        this.viewerOffsetY = canMove ? this.viewerTrackpadOffset(-gesture.y) : this.viewerEdgeOffset(-gesture.y * 0.35);
        const token = this.viewerToken;
        const settle = () => {
          this.viewerEdgeWheelTimer = null;
          if (token !== this.viewerToken || !this.viewer.open || gesture !== this.trackpadGesture || gesture.fired) return;
          gesture.fired = true;
          if (canMove && Math.abs(gesture.y) >= 80) this.finishViewerVerticalSwipe(gesture.y > 0 ? 1 : -1);
          else { this.viewerVerticalDragging = false; this.springViewerEdge(); }
        };
        const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? window.innerHeight : 1;
        const magnitude = Math.abs((event.deltaY || 0) * unit);
        gesture.peakY = Math.max(gesture.peakY || 0, magnitude);
        gesture.coastingY = magnitude > 0 && magnitude < gesture.peakY * 0.45 ? (gesture.coastingY || 0) + 1 : 0;
        if (canMove && Math.abs(gesture.y) >= 80 && gesture.coastingY >= 2) settle();
        else this.viewerEdgeWheelTimer = window.setTimeout(settle, 45);
      } else if (gesture.axis === "x" && gesture.x >= 82) {
        gesture.fired = true;
        this.revealViewerToolbar();
      }
    },

    viewerZoomSteps() {
      return [1, 1.25, 1.5, 2, 3];
    },

    nearestViewerZoomStep(value) {
      return this.viewerZoomSteps().reduce((closest, step) =>
        Math.abs(step - value) < Math.abs(closest - value) ? step : closest,
      1);
    },

    stepViewerZoom(direction, event = null) {
      const steps = this.viewerZoomSteps();
      const current = this.nearestViewerZoomStep(this.viewerZoom);
      const currentIndex = steps.indexOf(current);
      const nextIndex = Math.max(0, Math.min(steps.length - 1, currentIndex + (direction > 0 ? 1 : -1)));
      this.setViewerZoomLevel(steps[nextIndex], event);
    },

    setViewerZoomLevel(nextZoom, event = null, snap = true) {
      const requestedZoom = Math.max(1, Math.min(3, Number(nextZoom) || 1));
      const zoom = snap ? this.nearestViewerZoomStep(requestedZoom) : requestedZoom;
      this.viewerTransitionEnabled = true;
      if (zoom <= 1.02) {
        this.resetViewerTransform();
        return;
      }
      const previousZoom = Math.max(1, Number(this.viewerZoom) || 1);
      const stage = event?.currentTarget?.closest?.(".viewer-media") || event?.currentTarget;
      const rect = stage?.getBoundingClientRect?.();
      const anchorX = rect ? event.clientX - (rect.left + rect.width / 2) : 0;
      const anchorY = rect ? event.clientY - (rect.top + rect.height / 2) : 0;
      const ratio = zoom / previousZoom;
      this.viewerZoom = zoom;
      this.viewerOffsetX = (this.viewerOffsetX * ratio) - (anchorX * (ratio - 1));
      this.viewerOffsetY = (this.viewerOffsetY * ratio) - (anchorY * (ratio - 1));
      this.clampViewerTransform(stage);
    },

    clampViewerTransform(stage = null) {
      if (this.viewerZoom <= 1.02) {
        this.viewerZoom = 1;
        this.viewerOffsetX = 0;
        this.viewerOffsetY = 0;
        return;
      }
      const stageElement = stage || document.querySelector(".viewer-media.stage");
      const media = this.viewerVisibleMedia();
      if (!stageElement || !media) {
        return;
      }
      const stageRect = stageElement.getBoundingClientRect();
      const displayRect = this.viewerMediaDisplayRect(stageElement, media);
      const displayWidth = displayRect.width || stageRect.width;
      const displayHeight = displayRect.height || stageRect.height;
      const maxX = Math.max(0, (displayWidth * this.viewerZoom - stageRect.width) / 2 + 24);
      const maxY = Math.max(0, (displayHeight * this.viewerZoom - stageRect.height) / 2 + 24);
      this.viewerOffsetX = Math.max(-maxX, Math.min(maxX, this.viewerOffsetX));
      this.viewerOffsetY = Math.max(-maxY, Math.min(maxY, this.viewerOffsetY));
    },

    viewerPointerDown(event) {
      if (this.viewerSwipeFinish) this.viewerSwipeFinish();
      this.cancelViewerEdgeBounce();
      if (event.pointerType === "mouse" && event.button !== 0) return;
      if (event.currentTarget?.setPointerCapture) {
        event.currentTarget.setPointerCapture(event.pointerId);
      }
      this.viewerTransitionEnabled = false;
      const pointers = { ...this.viewerGesture.pointers, [event.pointerId]: { x: event.clientX, y: event.clientY } };
      this.viewerGesture.pointers = pointers;
      const values = Object.values(pointers);
      if (values.length >= 2) {
        this.viewerGesture.isPinch = true;
        this.viewerGesture.startDistance = this.pointerDistance(values[0], values[1]);
        this.viewerGesture.startZoom = this.viewerZoom;
        return;
      }
      this.viewerGesture.active = true;
      // Keep a pinch marked until every finger is released.
      this.viewerGesture.isPinch = false;
      this.viewerGesture.dismissAxis = "";
      this.viewerGesture.startX = event.clientX;
      this.viewerGesture.startY = event.clientY;
      this.viewerGesture.startedAt = Date.now();
      this.viewerGesture.startOffsetX = this.viewerOffsetX;
      this.viewerGesture.startOffsetY = this.viewerOffsetY;
      this.viewerGesture.moved = false;
    },

    viewerPointerMove(event) {
      if (!(event.pointerId in this.viewerGesture.pointers)) {
        return;
      }
      this.viewerGesture.pointers[event.pointerId] = { x: event.clientX, y: event.clientY };
      const values = Object.values(this.viewerGesture.pointers);
      if (values.length === 2 && this.viewerGesture.startDistance > 0) {
        const distance = this.pointerDistance(values[0], values[1]);
        const zoom = Math.max(1, Math.min(3, this.viewerGesture.startZoom * (distance / this.viewerGesture.startDistance)));
        this.viewerZoom = zoom;
        if (zoom === 1) {
          this.viewerOffsetX = 0;
          this.viewerOffsetY = 0;
        } else {
          this.clampViewerTransform(event.currentTarget);
        }
        return;
      }
      if (!this.viewerGesture.active) {
        return;
      }
      const deltaX = event.clientX - this.viewerGesture.startX;
      const deltaY = event.clientY - this.viewerGesture.startY;
      if (Math.abs(deltaX) > 8 || Math.abs(deltaY) > 8) {
        this.viewerGesture.moved = true;
      }
      if (this.viewerZoom > 1) {
        this.viewerOffsetX = this.viewerGesture.startOffsetX + deltaX;
        this.viewerOffsetY = this.viewerGesture.startOffsetY + deltaY;
        this.clampViewerTransform(event.currentTarget);
        return;
      }
      if (this.viewerGesture.isPinch) return;
      if (!this.viewerGesture.dismissAxis && Math.max(Math.abs(deltaX), Math.abs(deltaY)) > 12) {
        if (Math.abs(deltaY) > Math.abs(deltaX) * 1.2) this.viewerGesture.dismissAxis = "vertical";
        else if (Math.abs(deltaX) > Math.abs(deltaY) * 1.4) this.viewerGesture.dismissAxis = "horizontal";
      }
      this.viewerVerticalDragging = this.viewerGesture.dismissAxis === "vertical";
      this.viewerOffsetX = this.viewerGesture.dismissAxis === "vertical" ? 0
        : deltaX * (this.viewerGesture.dismissAxis === "horizontal" && deltaX > 0 ? 0.65 : 0.35);
      const canMove = deltaY < 0 ? this.canShowNextPair() : this.canShowPreviousPair();
      this.viewerOffsetY = this.viewerGesture.dismissAxis === "vertical"
        ? (canMove ? deltaY : this.viewerEdgeOffset(deltaY)) : deltaY * 0.25;
    },

    viewerPointerUp(event) {
      const wasPinch = this.viewerGesture.isPinch;
      if (event.currentTarget?.releasePointerCapture) {
        try {
          event.currentTarget.releasePointerCapture(event.pointerId);
        } catch (error) {
          void error;
        }
      }
      const pointer = this.viewerGesture.pointers[event.pointerId];
      delete this.viewerGesture.pointers[event.pointerId];
      if (!pointer) {
        return;
      }
      if (Object.keys(this.viewerGesture.pointers).length >= 1) {
        return;
      }
      const deltaX = event.clientX - this.viewerGesture.startX;
      const deltaY = event.clientY - this.viewerGesture.startY;
      const absX = Math.abs(deltaX);
      const absY = Math.abs(deltaY);
      if (absX > 8 || absY > 8) this.viewerGesture.moved = true;
      const moved = this.viewerGesture.moved;
      const elapsed = Math.max(1, Date.now() - (this.viewerGesture.startedAt || Date.now()));
      const atVerticalEdge = !wasPinch && this.viewerZoom <= 1.02
        && this.viewerGesture.dismissAxis === "vertical"
        && (deltaY < 0 ? !this.canShowNextPair() : !this.canShowPreviousPair());
      const verticalSwipe = absY >= 56 || (absY >= 24 && elapsed <= 250 && absY / elapsed >= 0.35);
      if (event.type === "pointercancel") {
        this.viewerSyntheticTapUntil = Date.now() + 450;
      } else if (wasPinch) {
        this.setViewerZoomLevel(this.viewerZoom, null, false);
      } else if (this.viewerZoom <= 1.02) {
        if (this.viewerGesture.dismissAxis === "horizontal" && deltaX >= 96 && deltaX > absY * 1.4) {
          this.viewerSyntheticTapUntil = Date.now() + 450;
          this.closeViewer();
        } else if (this.viewerGesture.dismissAxis === "horizontal" && deltaX <= -72 && -deltaX > absY * 1.4) {
          this.revealViewerToolbar();
        } else if (!atVerticalEdge && this.viewerGesture.dismissAxis === "vertical" && verticalSwipe && absY > absX * 1.2) {
          if (deltaY < 0) this.finishViewerVerticalSwipe(1); else this.finishViewerVerticalSwipe(-1);
        } else if (!this.viewerGesture.moved && event.pointerType !== "mouse") {
          this.viewerSyntheticTapUntil = Date.now() + 220;
          this.handleViewerTap(event.clientX, event.clientY, event.currentTarget);
        }
      }
      if (moved || wasPinch) this.viewerSyntheticTapUntil = Date.now() + 450;
      this.viewerGesture.active = false;
      this.viewerGesture.isPinch = false;
      if (this.viewerSwipeFinish) {
        this.viewerGesture.active = false;
        this.viewerGesture.isPinch = false;
        return;
      }
      this.viewerVerticalDragging = false;
      if (atVerticalEdge && !this.viewerClosing) {
        this.springViewerEdge();
      } else if (this.viewerZoom === 1 && !this.viewerClosing) {
        this.viewerOffsetX = 0;
        this.viewerOffsetY = 0;
      } else if (!this.viewerClosing) {
        this.clampViewerTransform(event.currentTarget);
      }
      window.requestAnimationFrame(() => {
        if (!this.viewerGesture.active && !this.viewerVerticalDragging) this.viewerTransitionEnabled = true;
      });
    },

    pointerDistance(left, right) {
      const deltaX = left.x - right.x;
      const deltaY = left.y - right.y;
      return Math.sqrt(deltaX * deltaX + deltaY * deltaY);
    },

    async refreshReview() {
      const payload = await this.api("/api/review/items");
      this.reviewItems = payload.items;
      this.lazyLoaded.review = true;
      this.sidebarCounts = { ...this.sidebarCounts, review: this.reviewItems.length };
    },

    async refreshDuplicates(force = false) {
      if (this.duplicateLoading) {
        return;
      }
      this.duplicateLoading = true;
      try {
        const payload = await this.api(force ? "/api/duplicates/items?force=true" : "/api/duplicates/items");
        this.applyDuplicatePayload(payload);
      } finally {
        this.duplicateLoading = false;
      }
    },

    applyDuplicatePayload(payload) {
      this.duplicateGroups = payload?.items || [];
      this.duplicateImageThreshold = Math.max(1, Number(payload?.image_threshold) || 2);
      this.duplicateHasCache = !!payload?.has_cache;
      this.duplicateCacheStale = !!payload?.cache_stale;
      this.duplicateCacheUpdatedAt = payload?.cache_updated_at || null;
      this.sidebarCounts = { ...this.sidebarCounts, duplicates: Number(payload?.active_total) || 0 };
      this.lazyLoaded.duplicates = true;
      const signatures = new Set(this.duplicateGroups.map((group) => group.signature));
      this.duplicateKeepSelections = Object.fromEntries(
        Object.entries(this.duplicateKeepSelections || {}).filter(([signature]) => signatures.has(signature)),
      );
      if (this.duplicateIgnoreConfirmSignature && !signatures.has(this.duplicateIgnoreConfirmSignature)) {
        this.duplicateIgnoreConfirmSignature = null;
      }
      if (this.duplicateCleanupConfirmSignature && !signatures.has(this.duplicateCleanupConfirmSignature)) {
        this.duplicateCleanupConfirmSignature = null;
      }
    },

    removeFoldersFromLocalGallery(folderNames) {
      const removed = new Set((folderNames || []).map((name) => String(name || "")).filter(Boolean));
      if (!removed.size) {
        return;
      }
      this.removeFolderFromRecentUpdates([...removed]);
      const items = (this.gallery.items || []).filter((item) => !removed.has(String(item?.folder_name || "")));
      this.gallery = {
        ...this.gallery,
        items,
        total: Math.max(0, Number(this.gallery.total || 0) - removed.size),
      };
      for (const folderName of removed) {
        this.invalidateDetailCache(folderName);
      }
      this.clearLocalGalleryCaches();
    },

    duplicateKeepSelection(signature) {
      return this.duplicateKeepSelections[String(signature)] || "";
    },

    duplicatePendingKey(kind, signature, suffix = "") {
      return [String(kind || "action"), String(signature || ""), String(suffix || "")].join(":");
    },

    duplicateActionPending(kind, signature, suffix = "") {
      return !!this.duplicatePendingActions[this.duplicatePendingKey(kind, signature, suffix)];
    },

    setDuplicateActionPending(kind, signature, suffix, pending) {
      const key = this.duplicatePendingKey(kind, signature, suffix);
      const next = { ...(this.duplicatePendingActions || {}) };
      if (pending) {
        next[key] = true;
      } else {
        delete next[key];
      }
      this.duplicatePendingActions = next;
    },

    removeDuplicateGroupLocally(signature) {
      const normalized = String(signature || "");
      this.duplicateGroups = (this.duplicateGroups || []).filter((group) => String(group.signature) !== normalized);
      this.sidebarCounts = { ...this.sidebarCounts, duplicates: this.duplicateGroups.length };
      this.cancelDuplicateKeep(normalized);
      if (this.duplicateCleanupConfirmSignature === normalized) {
        this.duplicateCleanupConfirmSignature = null;
      }
      if (this.duplicateIgnoreConfirmSignature === normalized) {
        this.duplicateIgnoreConfirmSignature = null;
      }
    },

    removeDuplicateFoldersLocally(signature, folderNames) {
      const normalized = String(signature || "");
      const removed = new Set((folderNames || []).map((name) => String(name || "")));
      const nextGroups = [];
      for (const group of this.duplicateGroups || []) {
        if (String(group.signature) !== normalized) {
          nextGroups.push(group);
          continue;
        }
        const items = (group.items || []).filter((item) => !removed.has(String(item.folder_name)));
        if (items.length < 2) {
          continue;
        }
        const matches = (group.matches || []).filter(
          (match) => !removed.has(String(match.left_folder_name)) && !removed.has(String(match.right_folder_name)),
        );
        nextGroups.push({
          ...group,
          items,
          matches,
          post_count: items.length,
          matched_image_count: matches.length,
        });
      }
      this.duplicateGroups = nextGroups;
      this.sidebarCounts = { ...this.sidebarCounts, duplicates: nextGroups.length };
    },

    applyDuplicateQueueFeedback(result) {
      const taskCount = Number(result?.sidebar_counts?.counts?.tasks);
      if (Number.isFinite(taskCount)) {
        this.sidebarCounts = { ...this.sidebarCounts, tasks: taskCount };
      }
      this.scheduleIdleWork(() => this.refreshTasks().catch(() => {}), 120);
    },

    selectDuplicateKeep(group, item) {
      if (!group?.signature || !item?.folder_name) {
        return;
      }
      this.duplicateKeepSelections = {
        ...this.duplicateKeepSelections,
        [String(group.signature)]: String(item.folder_name),
      };
      this.duplicateCleanupConfirmSignature = null;
      this.duplicateIgnoreConfirmSignature = null;
    },

    cancelDuplicateKeep(signature) {
      const next = { ...this.duplicateKeepSelections };
      delete next[String(signature)];
      this.duplicateKeepSelections = next;
    },

    askIgnoreDuplicate(signature) {
      this.duplicateIgnoreConfirmSignature = String(signature || "");
      this.duplicateCleanupConfirmSignature = null;
    },

    cancelIgnoreDuplicate() {
      this.duplicateIgnoreConfirmSignature = null;
    },

    askCleanupDuplicate(signature) {
      this.duplicateCleanupConfirmSignature = String(signature || "");
      this.duplicateIgnoreConfirmSignature = null;
    },

    cancelCleanupDuplicate() {
      this.duplicateCleanupConfirmSignature = null;
    },

    async confirmCleanupDuplicate(group) {
      const signature = String(group?.signature || "");
      const candidateCount = Math.max(0, Number(group?.cleanup_candidate_count) || 0);
      if (!signature || !candidateCount || this.duplicateCleanupConfirmSignature !== signature || this.duplicateActionPending("cleanup", signature)) {
        return;
      }
      const previousGroups = [...(this.duplicateGroups || [])];
      const previousDuplicateCount = Number(this.sidebarCounts.duplicates || 0);
      const affectedFolders = (group.items || []).map((item) => String(item.folder_name));
      this.setDuplicateActionPending("cleanup", signature, "", true);
      this.duplicateCleanupConfirmSignature = null;
      this.removeDuplicateGroupLocally(signature);
      this.notify("info", "已提交删除", "");
      try {
        const result = await this.api(`/api/duplicates/${encodeURIComponent(signature)}/cleanup`, {
          method: "POST",
          body: JSON.stringify({ confirmed: true }),
        });
        for (const folderName of affectedFolders) {
          this.invalidateDetailCache(folderName);
        }
        this.clearLocalGalleryCaches();
        this.applyDuplicateQueueFeedback(result);
      } catch (error) {
        this.duplicateGroups = previousGroups;
        this.sidebarCounts = { ...this.sidebarCounts, duplicates: previousDuplicateCount };
        this.notify("error", "清理失败", error.message || "请稍后重试。");
      } finally {
        this.setDuplicateActionPending("cleanup", signature, "", false);
      }
    },

    async confirmIgnoreDuplicate(group) {
      const signature = String(group?.signature || "");
      if (!signature || this.duplicateIgnoreConfirmSignature !== signature || this.duplicateActionPending("ignore", signature)) {
        return;
      }
      this.setDuplicateActionPending("ignore", signature, "", true);
      try {
        const result = await this.api(`/api/duplicates/${encodeURIComponent(signature)}/ignore`, {
          method: "POST",
          body: JSON.stringify({ confirmed: true }),
        });
        this.duplicateIgnoreConfirmSignature = null;
        if (result.duplicates) {
          this.applyDuplicatePayload(result.duplicates);
        }
        if (result.sidebar_counts) {
          this.applySidebarCounts(result.sidebar_counts);
        }
        this.notify("success", "已忽略重复项", "");
      } catch (error) {
        this.notify("error", "忽略失败", error.message || "请稍后重试。");
      } finally {
        this.setDuplicateActionPending("ignore", signature, "", false);
      }
    },

    async resolveDuplicateGroup(group) {
      const signature = String(group?.signature || "");
      const keepFolderName = this.duplicateKeepSelection(signature);
      if (!signature || !keepFolderName || this.duplicateActionPending("resolve", signature)) {
        return;
      }
      const previousGroups = [...(this.duplicateGroups || [])];
      const previousDuplicateCount = Number(this.sidebarCounts.duplicates || 0);
      const previousGallery = { ...this.gallery, items: [...(this.gallery.items || [])] };
      const previousRecentUpdates = this.cloneRecentUpdates();
      const previousRecentUpdateDisclosure = this.cloneRecentUpdateDisclosure();
      const removedFolders = (group.items || [])
        .map((item) => String(item.folder_name))
        .filter((folderName) => folderName !== keepFolderName);
      this.setDuplicateActionPending("resolve", signature, "", true);
      this.removeDuplicateGroupLocally(signature);
      this.removeFoldersFromLocalGallery(removedFolders);
      this.notify("info", "已提交删除", "");
      try {
        const result = await this.api(`/api/duplicates/${encodeURIComponent(signature)}/resolve`, {
          method: "POST",
          body: JSON.stringify({ keep_folder_name: keepFolderName, confirmed: true }),
        });
        this.cancelDuplicateKeep(signature);
        this.applyDuplicateQueueFeedback(result);
      } catch (error) {
        this.duplicateGroups = previousGroups;
        this.sidebarCounts = { ...this.sidebarCounts, duplicates: previousDuplicateCount };
        this.gallery = previousGallery;
        this.recentUpdates = previousRecentUpdates;
        this.restoreRecentUpdateDisclosure(previousRecentUpdateDisclosure);
        this.clearLocalGalleryCaches();
        this.notify("error", "重复内容处理失败", error.message || "请稍后重试。");
      } finally {
        this.setDuplicateActionPending("resolve", signature, "", false);
      }
    },

    async deleteDuplicateFolder(group, item) {
      const signature = String(group?.signature || "");
      const folderName = String(item?.folder_name || "");
      if (!signature || !folderName || this.duplicateActionPending("trash", signature, folderName)) {
        return;
      }
      const previousGroups = [...(this.duplicateGroups || [])];
      const previousDuplicateCount = Number(this.sidebarCounts.duplicates || 0);
      const previousGallery = { ...this.gallery, items: [...(this.gallery.items || [])] };
      const previousRecentUpdates = this.cloneRecentUpdates();
      const previousRecentUpdateDisclosure = this.cloneRecentUpdateDisclosure();
      this.setDuplicateActionPending("trash", signature, folderName, true);
      this.removeDuplicateFoldersLocally(signature, [folderName]);
      this.removeFoldersFromLocalGallery([folderName]);
      this.notify("info", "已提交删除", "");
      try {
        const result = await this.api(`/api/duplicates/${encodeURIComponent(signature)}/folders/${encodeURIComponent(folderName)}/trash`, {
          method: "POST",
        });
        this.applyDuplicateQueueFeedback(result);
      } catch (error) {
        this.duplicateGroups = previousGroups;
        this.sidebarCounts = { ...this.sidebarCounts, duplicates: previousDuplicateCount };
        this.gallery = previousGallery;
        this.recentUpdates = previousRecentUpdates;
        this.restoreRecentUpdateDisclosure(previousRecentUpdateDisclosure);
        this.clearLocalGalleryCaches();
        this.notify("error", "删除失败", error.message || "请稍后重试。");
      } finally {
        this.setDuplicateActionPending("trash", signature, folderName, false);
      }
    },

    async refreshTasks() {
      const payload = await this.api("/api/tasks/runs");
      this.taskRuns = payload.items;
      this.queuedTasks = payload.queue || [];
      this.lazyLoaded.tasks = true;
      this.sidebarCounts = { ...this.sidebarCounts, tasks: this.taskRuns.length };
      if (this.queuedCancelConfirmId && !this.queuedTasks.some((item) => Number(item.queue_id) === this.queuedCancelConfirmId)) {
        this.queuedCancelConfirmId = null;
      }
    },

    async refreshRecentUpdates() {
      if (this.recentUpdatesLoading) {
        // 加载中收到新的刷新请求（例如切换来源页签）时记一次待办，结束后补跑，避免停留在旧来源的数据。
        this.recentUpdatesPendingRefresh = true;
        return;
      }
      const requestId = ++this.recentUpdatesRequestId;
      const requestKind = ["all", "up", "site", "xhs"].includes(this.sourceKind) ? this.sourceKind : "all";
      this.recentUpdatesLoading = true;
      try {
        const params = new URLSearchParams({ limit: "50" });
        if (requestKind !== "all") {
          params.set("source_kind", requestKind);
        }
        const payload = await this.api(`/api/gallery/recent-updates?${params.toString()}`);
        if (requestId !== this.recentUpdatesRequestId || requestKind !== this.sourceKind) {
          return;
        }
        const groups = Array.isArray(payload?.groups) ? payload.groups : [];
        this.reconcileRecentUpdateDisclosure(groups);
        this.recentUpdates = {
          groups,
          total_groups: Number(payload?.total_groups) || groups.length,
          total_items: Number(payload?.total_items) || groups.reduce((total, group) => total + (group.items || []).length, 0),
        };
        this.lazyLoaded.recentUpdates = true;
        this.$nextTick(() => this.scheduleRecentUpdatesMeasure());
      } finally {
        if (requestId === this.recentUpdatesRequestId) {
          this.recentUpdatesLoading = false;
        }
        if (this.recentUpdatesPendingRefresh) {
          this.recentUpdatesPendingRefresh = false;
          this.refreshRecentUpdates();
        }
      }
    },

    recentUpdatesEmptyHint() {
      // 与搜索结果的“来源限定”提示同思路：过滤后为空时把原因摆到台面上，并告诉用户怎么切回去。
      if (this.currentView !== "recent-updates" || this.sourceKind === "all") {
        return "";
      }
      const option = this.sourceKindOptions.find((item) => item.key === this.sourceKind);
      return `仅显示「${option?.label || this.sourceKind}」来源的更新，试试切换回「所有项目」`;
    },

    recentUpdateGroupTime(group) {
      return this.formatDateTime(group?.updated_at || group?.finished_at || group?.created_at) || "未知时间";
    },

    recentUpdateTaskLabel(group) {
      return group?.task_type === 'xhs-sync' ? '小红书同步' : group?.task_type === "site-sync" ? "站点同步" : "B站拉取";
    },

    recentUpdateTaskKey(taskId) {
      return String(taskId ?? "");
    },

    recentUpdateGroupKey(group) {
      return this.recentUpdateTaskKey(group?.task_id);
    },

    isRecentUpdateExpanded(group) {
      return !!this.recentUpdateExpanded[this.recentUpdateGroupKey(group)];
    },

    isRecentUpdateExpandable(group) {
      return !!this.recentUpdateExpandable[this.recentUpdateGroupKey(group)];
    },

    isRecentUpdateCollapsed(group) {
      return this.isRecentUpdateExpandable(group) && !this.isRecentUpdateExpanded(group);
    },

    recentUpdateGridStyle(group) {
      const height = Number(this.recentUpdateRowHeights[this.recentUpdateGroupKey(group)] || 0);
      return height > 0 ? `--recent-update-collapsed-height: ${height}px;` : "";
    },

    resetRecentUpdateDisclosure() {
      this.recentUpdateExpanded = {};
      this.recentUpdateExpandable = {};
      this.recentUpdateRowHeights = {};
    },

    reconcileRecentUpdateDisclosure(groups) {
      const activeKeys = new Set((groups || []).map((group) => this.recentUpdateGroupKey(group)));
      const filterActive = (state) => Object.fromEntries(
        Object.entries(state).filter(([key]) => activeKeys.has(key)),
      );
      this.recentUpdateExpanded = filterActive(this.recentUpdateExpanded);
      this.recentUpdateExpandable = filterActive(this.recentUpdateExpandable);
      this.recentUpdateRowHeights = filterActive(this.recentUpdateRowHeights);
    },

    expandRecentUpdate(taskId) {
      const key = this.recentUpdateTaskKey(taskId);
      if (!this.recentUpdateExpandable[key] || this.recentUpdateExpanded[key]) {
        return;
      }
      this.recentUpdateExpanded = {
        ...this.recentUpdateExpanded,
        [key]: true,
      };
    },

    collapseRecentUpdate(taskId) {
      const key = this.recentUpdateTaskKey(taskId);
      if (!this.recentUpdateExpanded[key]) {
        return;
      }
      const nextExpanded = { ...this.recentUpdateExpanded };
      delete nextExpanded[key];
      this.recentUpdateExpanded = nextExpanded;
      this.$nextTick(() => this.scheduleRecentUpdatesMeasure());
    },

    scheduleRecentUpdatesMeasure() {
      if (this.currentView !== "recent-updates" || this.recentUpdateMeasureRaf !== null) {
        return;
      }
      const requestFrame = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 0));
      this.recentUpdateMeasureRaf = requestFrame(() => {
        this.recentUpdateMeasureRaf = null;
        this.measureRecentUpdates();
      });
    },

    measureRecentUpdates() {
      if (this.currentView !== "recent-updates") {
        return;
      }
      const grids = Array.from(document.querySelectorAll("[data-recent-update-grid]"));
      if (!grids.length) {
        return;
      }
      const nextExpandable = { ...this.recentUpdateExpandable };
      const nextRowHeights = { ...this.recentUpdateRowHeights };
      let measured = false;
      grids.forEach((grid) => {
        const key = this.recentUpdateTaskKey(grid.dataset?.recentUpdateTaskId);
        if (!key) {
          return;
        }
        const gridRect = grid.getBoundingClientRect();
        if (!gridRect.width || !gridRect.height) {
          return;
        }
        const cards = Array.from(grid.querySelectorAll(".recent-update-card"));
        if (!cards.length) {
          return;
        }
        const firstTop = cards[0].getBoundingClientRect().top;
        const firstRowCards = cards.filter((card) => Math.abs(card.getBoundingClientRect().top - firstTop) <= 1);
        const firstRowBottom = Math.max(
          ...firstRowCards.map((card) => card.getBoundingClientRect().bottom - gridRect.top),
        );
        const hasSecondRow = cards.some((card) => card.getBoundingClientRect().top - firstTop > 1);
        nextExpandable[key] = hasSecondRow;
        if (firstRowBottom > 0) {
          nextRowHeights[key] = Math.ceil(firstRowBottom);
        }
        if (!hasSecondRow) {
          delete nextExpandable[key];
          delete nextRowHeights[key];
        }
        measured = true;
      });
      if (!measured) {
        return;
      }
      const activeKeys = new Set((this.recentUpdates?.groups || []).map((group) => this.recentUpdateGroupKey(group)));
      const nextExpanded = Object.fromEntries(
        Object.entries(this.recentUpdateExpanded).filter(([key, expanded]) => expanded && activeKeys.has(key)),
      );
      const mapsEqual = (left, right) => {
        const leftKeys = Object.keys(left);
        const rightKeys = Object.keys(right);
        return leftKeys.length === rightKeys.length && leftKeys.every((key) => left[key] === right[key]);
      };
      if (!mapsEqual(this.recentUpdateExpandable, nextExpandable)) {
        this.recentUpdateExpandable = nextExpandable;
      }
      if (!mapsEqual(this.recentUpdateRowHeights, nextRowHeights)) {
        this.recentUpdateRowHeights = nextRowHeights;
      }
      if (!mapsEqual(this.recentUpdateExpanded, nextExpanded)) {
        this.recentUpdateExpanded = nextExpanded;
      }
    },

    recentUpdateImageText(item) {
      const imageCount = Number(item?.image_count || item?.asset_count || 0);
      const livephotoCount = Number(item?.livephoto_count || 0);
      if (imageCount && livephotoCount) {
        return `${imageCount} 图 · ${livephotoCount} 动图`;
      }
      if (livephotoCount) {
        return `${livephotoCount} 个 Live Photo`;
      }
      return `${imageCount} 张图片`;
    },

    cloneRecentUpdates() {
      return {
        ...this.recentUpdates,
        groups: (this.recentUpdates?.groups || []).map((group) => ({
          ...group,
          items: [...(group.items || [])],
        })),
      };
    },

    cloneRecentUpdateDisclosure() {
      return {
        expanded: { ...this.recentUpdateExpanded },
        expandable: { ...this.recentUpdateExpandable },
        rowHeights: { ...this.recentUpdateRowHeights },
      };
    },

    restoreRecentUpdateDisclosure(snapshot) {
      if (!snapshot) {
        return;
      }
      this.recentUpdateExpanded = { ...(snapshot.expanded || {}) };
      this.recentUpdateExpandable = { ...(snapshot.expandable || {}) };
      this.recentUpdateRowHeights = { ...(snapshot.rowHeights || {}) };
    },

    removeFolderFromRecentUpdates(folderNames) {
      const removed = new Set(
        (Array.isArray(folderNames) ? folderNames : [folderNames])
          .map((name) => String(name || ""))
          .filter(Boolean),
      );
      if (!removed.size || !this.recentUpdates?.groups?.length) {
        return;
      }
      const groups = (this.recentUpdates.groups || [])
        .map((group) => ({
          ...group,
          items: (group.items || []).filter((item) => !removed.has(String(item?.folder_name || ""))),
        }))
        .filter((group) => group.items.length);
      this.recentUpdates = {
        ...this.recentUpdates,
        groups,
        total_groups: groups.length,
        total_items: groups.reduce((total, group) => total + group.items.length, 0),
      };
      this.reconcileRecentUpdateDisclosure(groups);
      this.$nextTick(() => this.scheduleRecentUpdatesMeasure());
    },

    visibleTaskRuns() {
      const runs = this.taskRuns || [];
      if (this.taskHistoryExpanded) {
        return runs;
      }
      const attention = runs.filter((item) => item.status !== "success");
      const recentSuccess = runs.filter((item) => item.status === "success").slice(0, 3);
      return [...attention, ...recentSuccess]
        .filter((item, index, items) => items.findIndex((candidate) => candidate.id === item.id) === index)
        .sort((left, right) => Number(right.id || 0) - Number(left.id || 0));
    },

    hasHiddenTaskRuns() {
      return !this.taskHistoryExpanded && this.visibleTaskRuns().length < (this.taskRuns || []).length;
    },

    taskSummary(item) {
      const message = String(item?.message || item?.status || "");
      if (item?.status === "success" || item?.status === "running") {
        return message;
      }
      if (/timed out|timeout|超时/i.test(message)) {
        return "请求超时，请检查网络或提高超时设置。";
      }
      return "任务未完成，请查看日志与细节后重试。";
    },

    formatInspectorJson(payload) {
      try {
        return JSON.stringify(payload || {}, null, 2);
      } catch (error) {
        return String(payload || "");
      }
    },

    formatTaskDetail(taskLike = {}) {
      const details = taskLike.details || {};
      const stats = taskLike.stats || details || {};
      const taskDetail = taskLike.task_detail || details.task_detail || {};
      const events = taskLike.events || details.events || [];
      const completed = Array.isArray(taskDetail.completed) ? taskDetail.completed : [];
      const counters = this.taskCounterTags({ ...taskLike, stats, counters: taskLike.counters || details.counters || stats.counters || {} });
      const lines = [
        `目标：${taskDetail.target || taskLike.target || this.taskTargetText(taskLike)}`,
        `正在做：${taskDetail.doing || taskLike.current_step || taskLike.message || "已结束"}`,
        `即将做：${taskDetail.next || taskLike.next_step || this.taskNextText(taskLike)}`,
        `进度：${this.taskProgressText(taskLike)}`,
        "",
        "已完成：",
      ];
      if (completed.length) {
        completed.forEach((item, index) => lines.push(`${index + 1}. ${item}`));
      } else if (taskLike.status && taskLike.status !== "running") {
        lines.push(`1. ${taskLike.message || taskLike.status}`);
      } else {
        lines.push("暂无完成项，任务刚开始或正在准备范围。");
      }
      if (counters.length) {
        lines.push("", "量化统计：");
        counters.forEach((item) => lines.push(`${item.label}：${item.value}`));
      }
      if (Array.isArray(stats.added_items) && stats.added_items.length) {
        lines.push("", `本次新增：${stats.added_items.length} 条`);
      }
      if (Array.isArray(stats.resolution_diagnostics) && stats.resolution_diagnostics.length) {
        lines.push("", "解析诊断：", this.formatInspectorJson(stats.resolution_diagnostics));
      }
      if (Array.isArray(stats.issues) && stats.issues.length) {
        const title = (taskLike.mode || taskLike.task_type) === "xhs-authors" ? "作者校验问题：" : "实况配对问题（最多显示 50 项）：";
        lines.push("", title, this.formatInspectorJson(stats.issues));
      }
      if ((taskLike.mode || taskLike.task_type) === "xhs-authors") {
        lines.push("", `全库检查 ${Number(stats.checked) || 0} 篇笔记，修复 ${Number(stats.repaired) || 0} 篇，正常 ${Number(stats.unchanged) || 0} 篇，待处理 ${Number(stats.unresolved) || 0} 篇`);
        (stats.issues || []).forEach((issue) => lines.push(this.xhsAuthText(issue.reason)));
        if (stats.unresolved_notes?.length) {
          lines.push("", "未能找到作者昵称的笔记：", this.formatInspectorJson(stats.unresolved_notes));
        }
      }
      if (events.length) {
        lines.push("", "事件日志：");
        events.slice(-40).forEach((event, index) => {
          const context = Object.entries(event)
            .filter(([key]) => !["at", "message"].includes(key))
            .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(",") : value}`)
            .join(" · ");
          lines.push(`${index + 1}. ${event.at || ""} ${event.message || ""}${context ? ` · ${context}` : ""}`.trim());
        });
      }
      return lines.join("\n");
    },

    taskTargetText(taskLike = {}) {
      const mode = taskLike.mode || taskLike.task_type;
      return {
        pull: "拉取订阅动态并刷新图库",
        "subscription-pull": "拉取当前订阅动态",
        "subscription-reload": "全量校验并拉取当前订阅",
        review: "处理待审核内容",
        validate: "校验图库完整性并修复缺失内容",
        index: "重建页面索引",
        "thumbnail-rebuild": "重建缩略图",
        "site-sync": "同步站点订阅",
        "xhs-import": "导入小红书点赞清单",
        "xhs-sync": "同步小红书本人点赞与媒体",
        "xhs-verify": "检查小红书本地归档完整性",
        "xhs-authors": "全库校验小红书作者昵称",
        "livephoto-check": "检查实况文件配对，不改动媒体",
        "livephoto-repair": "为可确定配对的实况文件补写配对标识",
        "livephoto-repair-errors": "自动找回实况原图并确认配对",
        "livephoto-manual": "按人工确认结果写入实况配对标识",
        "site-validate": "全量校验站点订阅",
        "storage-cleanup": "清理垃圾文件",
        startup: "启动后整理图库",
      }[mode] || "执行队列任务";
    },

    taskNextText(taskLike = {}) {
      if (taskLike.cancel_requested) {
        return "停止当前执行步骤并释放队列";
      }
      if (taskLike.status && taskLike.status !== "running") {
        return this.canRetryTask(taskLike) ? "可重新提交任务" : "无后续步骤";
      }
      return "继续执行队列中的下一步";
    },

    formatAddedItems(items) {
      const rows = Array.isArray(items) ? items : [];
      if (!rows.length) {
        return "本次没有新增内容。";
      }
      return rows
        .map((item, index) => {
          const lines = [
            `${index + 1}. ${item.title || `动态 ${item.top_dynamic_id}`}`,
            `   订阅: ${item.subscription_name || item.subscription_uid || "默认订阅"}`,
            `   时间: ${item.pub_time || "-"}`,
            `   类型: ${item.change_type === "updated" ? "补同步/更新" : "新增"}`,
            `   静态图: ${item.image_count || 0} · Live Photo: ${item.livephoto_count || 0} · 文件: ${item.saved_files || 0}`,
            `   动态ID: ${item.top_dynamic_id || "-"}`,
          ];
          return lines.join("\n");
        })
        .join("\n\n");
    },

    openCurrentTaskInspector() {
      this.taskInspector = {
        open: true,
        title: this.pullStatus.running ? "当前运行任务" : "当前状态",
        subtitle: this.pullStatus.message || "空闲",
        body: this.formatTaskDetail(this.pullStatus),
      };
      this.taskInspectorLoading = false;
      this.syncBodyLock();
    },

    openCurrentAddedItems() {
      const items = this.pullStatus?.stats?.added_items || [];
      this.taskInspector = {
        open: true,
        title: "本次新增内容",
        subtitle: this.pullStatus.message || "当前任务",
        body: this.formatAddedItems(items),
      };
      this.taskInspectorLoading = false;
      this.syncBodyLock();
    },

    async openTaskInspector(item) {
      this.taskInspectorLoading = true;
      this.taskInspector = {
        open: true,
        title: `${item.task_type} #${item.id}`,
        subtitle: item.message || item.status,
        body: "正在加载日志与执行细节...",
      };
      this.syncBodyLock();
      try {
        const payload = await this.api(`/api/tasks/${item.id}`);
        const task = payload.item || item;
        this.taskInspector = {
          open: true,
          title: `${task.task_type} #${task.id}`,
          subtitle: task.message || task.status,
          body: this.formatTaskDetail(task),
        };
      } catch (error) {
        this.taskInspector = {
          open: true,
          title: `${item.task_type} #${item.id}`,
          subtitle: item.message || item.status,
          body: `加载任务详情失败：${error.message}`,
        };
      } finally {
        this.taskInspectorLoading = false;
      }
    },

    async openTaskAddedItems(item) {
      this.taskInspectorLoading = true;
      this.taskInspector = {
        open: true,
        title: `任务 #${item.id} 的新增内容`,
        subtitle: item.message || item.status,
        body: "正在加载本次新增列表...",
      };
      this.syncBodyLock();
      try {
        const payload = await this.api(`/api/tasks/${item.id}`);
        const task = payload.item || item;
        const items = task?.details?.added_items || [];
        this.taskInspector = {
          open: true,
          title: `任务 #${task.id} 的新增内容`,
          subtitle: task.message || task.status,
          body: this.formatAddedItems(items),
        };
      } catch (error) {
        this.taskInspector = {
          open: true,
          title: `任务 #${item.id} 的新增内容`,
          subtitle: item.message || item.status,
          body: `加载新增内容失败：${error.message}`,
        };
      } finally {
        this.taskInspectorLoading = false;
      }
    },

    openQueuedTaskInspector(item) {
      this.taskInspector = {
        open: true,
        title: `排队任务 #${item.queue_id}`,
        subtitle: item.label || item.kind,
        body: [
          `目标：${item.label || this.taskTargetText({ mode: item.kind })}`,
          `正在做：等待前序任务完成`,
          `即将做：排到第 ${item.position || "-"} 位后开始执行`,
          `进度：排队中`,
          "",
          `任务类型：${this.taskModeLabel(item.kind)}`,
          `入队时间：${item.queued_at || "-"}`,
        ].join("\n"),
      };
      this.taskInspectorLoading = false;
      this.syncBodyLock();
    },

    closeTaskInspector() {
      this.taskInspector = { open: false, title: "", subtitle: "", body: "" };
      this.taskInspectorLoading = false;
      this.syncBodyLock();
    },

    taskHasAddedItems(item) {
      return Array.isArray(item?.details?.added_items) && item.details.added_items.length > 0;
    },

    currentTaskHasAddedItems() {
      return Array.isArray(this.pullStatus?.stats?.added_items) && this.pullStatus.stats.added_items.length > 0;
    },

    taskModeLabel(mode) {
      const labels = {
        pull: "拉取",
        "subscription-pull": "订阅拉取",
        "subscription-reload": "全量订阅",
        review: "待审核",
        validate: "校验",
        index: "索引",
        "data-upgrade": "数据维护",
        "thumbnail-rebuild": "缩略图",
        thumbnails: "缩略图",
        "site-sync": "站点同步",
        "xhs-import": "小红书导入",
        "xhs-sync": "小红书同步",
        "xhs-verify": "小红书归档检查",
        "xhs-authors": "小红书作者校验",
        "livephoto-check": "实况配对检查",
        "livephoto-repair": "修复实况配对",
        "livephoto-repair-errors": "修复实况异常",
        "livephoto-manual": "确认实况配对",
        "site-validate": "站点校验",
        "storage-cleanup": "存储清理",
        icons: "图标刷新",
        "subscription-icons": "动态图标",
        "site-icons": "站点图标",
        "duplicate-trash": "重复贴文删除",
        "xhs-unlike": "小红书取消点赞",
        "duplicate-resolve": "仅保留贴文",
        "duplicate-cleanup": "重复图片清理",
        startup: "启动整理",
        idle: "空闲",
      };
      return labels[mode] || mode || "任务";
    },

    taskProgressValue(status) {
      const explicit = Number(status?.progress);
      if (Number.isFinite(explicit)) {
        return Math.max(0, Math.min(100, explicit));
      }
      const processed = Number(status?.processed);
      const total = Number(status?.total);
      if (Number.isFinite(processed) && Number.isFinite(total) && total > 0) {
        return Math.max(0, Math.min(100, Math.round((processed / total) * 100)));
      }
      return status?.running ? 8 : 0;
    },

    taskProgressText(status) {
      const processed = Number(status?.processed);
      const total = Number(status?.total);
      if (Number.isFinite(processed) && Number.isFinite(total) && total > 0) {
        return `${Math.min(processed, total)} / ${total} · ${this.taskProgressValue(status)}%`;
      }
      return status?.running ? `${status.current_step || status.message || "执行中"} · ${this.taskProgressValue(status)}%` : "无运行任务";
    },

    taskCounterTags(status) {
      const stats = status?.stats || {};
      const counters = status?.counters || status?.site_detail?.counters || {};
      const rows = [
        ["发现", counters.discovered ?? stats.matched],
        ["入库", counters.posts ?? stats.downloaded_candidates],
        ["下载", counters.downloaded ?? stats.saved_files],
        ["拦截", counters.blocked ?? stats.review_candidates],
        ["跳过", counters.skipped ?? stats.skipped],
        ["失败", counters.errors ?? stats.errors],
        ["检查组数", stats.checked],
        ["已配对", stats.paired],
        ["本次修复", stats.repaired],
        ["需修复", stats.needs_repair],
        ["缺失或歧义", stats.unpaired],
      ];
      return rows
        .filter(([, value]) => value !== undefined && value !== null)
        .map(([label, value]) => ({ label, value: Number(value) || 0 }));
    },

    async pauseTask() {
      this.setImmediateTaskFeedback("正在暂停当前任务...", { paused: true });
      const result = await this.api("/api/tasks/pause", { method: "POST" });
      await this.refreshStatus();
      await this.refreshTasks();
      this.notify("info", "任务已暂停", result.message);
    },

    async resumeTask() {
      this.setImmediateTaskFeedback("正在继续当前任务...", { paused: false });
      const result = await this.api("/api/tasks/resume", { method: "POST" });
      await this.refreshStatus();
      await this.refreshTasks();
      this.notify("success", "任务已继续", result.message);
    },

    async cancelTask() {
      this.setImmediateTaskFeedback("正在请求取消当前任务...", { cancel_requested: true });
      const result = await this.api("/api/tasks/cancel", { method: "POST" });
      await this.refreshStatus();
      await this.refreshTasks();
      this.notify("info", "已请求取消", result.message);
    },

    clearTaskLogsButtonLabel() {
      return this.clearTaskLogsConfirmStep === 0 ? "清理已结束日志" : "再次确认清理";
    },

    askClearTaskLogs() {
      this.clearTaskLogsConfirmStep = Math.min(this.clearTaskLogsConfirmStep + 1, 1);
    },

    cancelClearTaskLogs() {
      this.clearTaskLogsConfirmStep = 0;
    },

    async confirmClearTaskLogs() {
      if (this.clearTaskLogsConfirmStep < 1) {
        return;
      }
      const previousTaskRuns = [...(this.taskRuns || [])];
      const previousRecentUpdates = this.cloneRecentUpdates();
      const previousRecentUpdateDisclosure = this.cloneRecentUpdateDisclosure();
      this.taskRuns = (this.taskRuns || []).filter((item) => item.status === "running");
      this.clearTaskLogsConfirmStep = 0;
      this.notify("info", "正在清理任务日志", "页面已先更新，后台正在处理。");
      try {
        const result = await this.api("/api/tasks/clear-finished", { method: "POST" });
        await Promise.all([
          this.refreshTasks(),
          this.lazyLoaded.recentUpdates || this.currentView === "recent-updates" ? this.refreshRecentUpdates() : Promise.resolve(),
        ]);
        this.notify("success", "任务日志已清理", result.message);
      } catch (error) {
        this.taskRuns = previousTaskRuns;
        this.recentUpdates = previousRecentUpdates;
        this.restoreRecentUpdateDisclosure(previousRecentUpdateDisclosure);
        this.notify("error", "清理失败", error.message || "任务日志已恢复。");
      }
    },

    async refreshTrash() {
      const payload = await this.api("/api/trash/items");
      this.trashItems = payload.items;
      this.lazyLoaded.trash = true;
      this.sidebarCounts = { ...this.sidebarCounts, trash: this.trashItems.length };
      const previousExpanded = { ...(this.trashGroupExpanded || {}) };
      this.trashGroupExpanded = Object.fromEntries(
        this.trashGroups().map((group) => [group.key, previousExpanded[group.key] ?? false]),
      );
    },

    trashGroupKey(item) {
      const uid = String(item?.subscription_uid || "").trim();
      if (uid) {
        return uid;
      }
      const name = String(item?.subscription_name || "").trim();
      return name ? `name:${name}` : "unknown";
    },

    trashGroups() {
      const groups = new Map();
      for (const item of this.trashItems || []) {
        const query = this.trashSearch.trim().toLowerCase();
        if (query && ![item.folder_name, item.folder?.title, item.subscription_name, item.deleted_at].join(" ").toLowerCase().includes(query)) continue;
        const key = this.trashGroupKey(item);
        const uid = String(item?.subscription_uid || "").trim();
        const name = String(item?.subscription_name || "").trim()
          || (uid.startsWith("site:") ? "站点订阅" : uid ? `UID ${uid}` : "未归属内容");
        if (!groups.has(key)) {
          groups.set(key, {
            key,
            uid,
            name,
            is_site: uid.startsWith("site:"),
            latest_deleted_at: item.deleted_at || "",
            items: [],
          });
        }
        const group = groups.get(key);
        group.items.push(item);
        if (String(item.deleted_at || "") > String(group.latest_deleted_at || "")) {
          group.latest_deleted_at = item.deleted_at || "";
        }
      }
      return [...groups.values()].sort((left, right) => String(right.latest_deleted_at || "").localeCompare(String(left.latest_deleted_at || "")));
    },

    isTrashGroupExpanded(key) {
      return this.trashGroupExpanded[String(key)] === true;
    },

    toggleTrashGroup(key) {
      const normalized = String(key);
      this.trashGroupExpanded = {
        ...this.trashGroupExpanded,
        [normalized]: !this.isTrashGroupExpanded(normalized),
      };
    },

    trashGroupIconUrl(group) {
      if (!group?.uid) {
        return "";
      }
      const item = (this.subscriptions || []).find((entry) => String(entry.uid) === String(group.uid));
      return item ? this.subscriptionIconUrl(item) : "";
    },

    currentViewerDeleteKey() {
      if (!this.viewer.folder?.folder_name || !this.viewer.pair?.pair_index) {
        return null;
      }
      return `${this.viewer.folder.folder_name}::${this.viewer.pair.pair_index}`;
    },

    pairDeleteKey(folderName, pairIndex) {
      if (!folderName || !pairIndex) {
        return null;
      }
      return `${folderName}::${pairIndex}`;
    },

    pairPlaceholderKey(folderName, pairIndex) {
      return `${folderName}:${pairIndex}`;
    },

    pairDeletePending(folderName, pairIndex) {
      return this.deletePairConfirmKey === this.pairDeleteKey(folderName, pairIndex) && this.deletePairConfirmStep >= 1;
    },

    deletePairButtonLabel() {
      if (this.deletePairConfirmStep === 0) {
        return "删除";
      }
      return "确认永久删除";
    },

    askDeleteViewerPair() {
      const key = this.currentViewerDeleteKey();
      if (!key) {
        return;
      }
      this.askDeletePair(this.viewer.folder.folder_name, this.viewer.pair.pair_index);
    },

    askDeletePair(folderName, pairIndex) {
      const key = this.pairDeleteKey(folderName, pairIndex);
      if (!key) {
        return;
      }
      this.deletePairConfirmKey = key;
      this.deletePairConfirmStep = 1;
    },

    cancelDeleteViewerPair() {
      this.deletePairConfirmKey = null;
      this.deletePairConfirmStep = 0;
    },

    async confirmDeleteViewerPair() {
      const folderName = this.viewer.folder?.folder_name;
      const pairIndex = this.viewer.pair?.pair_index;
      const key = this.currentViewerDeleteKey();
      if (!folderName || !pairIndex || this.deletePairConfirmKey !== key || this.deletePairConfirmStep < 1) {
        return;
      }
      await this.confirmDeletePair(folderName, pairIndex);
    },

    pairAssetDelta(pair) {
      const hasImage = !!(pair?.image || pair?.has_images);
      const hasLivephoto = !!(pair?.livephoto || pair?.has_livephoto);
      return {
        images: hasImage ? 1 : 0,
        livephotos: hasLivephoto ? 1 : 0,
        total: (hasImage ? 1 : 0) + (hasLivephoto ? 1 : 0),
      };
    },

    async confirmDeletePair(folderName, pairIndex) {
      const key = this.pairDeleteKey(folderName, pairIndex);
      if (!folderName || !pairIndex || this.deletePairConfirmKey !== key || this.deletePairConfirmStep < 1 || this.pairDeletingKey) {
        return;
      }
      this.pairDeletingKey = key;
      const currentIndex = this.viewerIndex;
      const previousDetail = {
        open: this.detail.open,
        pairs: [...(this.detail.pairs || [])],
        folder: this.detail.folder ? { ...this.detail.folder } : null,
        videos: [...(this.detail.videos || [])],
      };
      const previousGallery = {
        items: [...(this.gallery.items || [])],
        total: this.gallery.total || 0,
      };
      const previousCache = this.detailCache[folderName] ? this.cloneDetailPayload(this.detailCache[folderName]) : null;
      const removedPair = (this.detail.pairs || []).find((pair) => Number(pair.pair_index) === Number(pairIndex))
        || (previousCache?.pairs || []).find((pair) => Number(pair.pair_index) === Number(pairIndex))
        || (this.gallery.items || []).find((item) => item.folder_name === folderName && Number(item.pair_index) === Number(pairIndex));
      const delta = this.pairAssetDelta(removedPair);
      const nextDetailPairs = (this.detail.pairs || []).filter((pair) => Number(pair.pair_index) !== Number(pairIndex));
      const nextSequence = (this.viewerSequence || []).filter(
        (entry) => !(entry.folder?.folder_name === folderName && Number(entry.pair?.pair_index) === Number(pairIndex)),
      );
      this.cancelDeleteViewerPair();
      if (this.detail.folder?.folder_name === folderName) {
        this.detail = {
          open: true,
          pairs: nextDetailPairs,
          folder: this.detail.folder
            ? {
                ...this.detail.folder,
                image_count: Math.max(0, (this.detail.folder.image_count || 0) - delta.images),
                livephoto_count: Math.max(0, (this.detail.folder.livephoto_count || 0) - delta.livephotos),
                asset_count: Math.max(0, (this.detail.folder.asset_count || 0) - delta.total),
              }
            : this.detail.folder,
          videos: this.detail.videos || [],
        };
        this.cacheDetailPayload(folderName, this.detail);
      }
      if (this.galleryViewMode === "pair") {
        this.removePairFromGallery(folderName, pairIndex, { placeholder: true });
      }
      if (this.detail.folder?.folder_name !== folderName) {
        this.removeCachedDetailPair(folderName, pairIndex);
      }
      if (this.viewer.open && this.viewer.folder?.folder_name === folderName && Number(this.viewer.pair?.pair_index) === Number(pairIndex)) {
        if (!nextSequence.length) {
          this.closeViewer();
        } else {
          this.viewerSequence = nextSequence;
          this.viewerIndex = Math.min(currentIndex, nextSequence.length - 1);
          this.openViewerEntry(nextSequence[this.viewerIndex], true, false);
        }
      } else if (this.viewer.open) {
        this.viewerSequence = nextSequence;
      }
      this.notify("info", "已提交删除", "");
      try {
        const result = await this.api(`/api/gallery/folders/${encodeURIComponent(folderName)}/pairs/${pairIndex}/delete`, {
          method: "POST",
        });
        this.clearLocalGalleryCaches();
        if (result.remove_empty_folder) {
          this.removeFolderFromGallery(folderName);
          this.invalidateDetailCache(folderName);
          if (this.detail.folder?.folder_name === folderName && !nextDetailPairs.length) {
            this.closeDetail();
          }
        }
        this.schedulePostMutationRefresh(result, ["all", "favorites", "livephoto", "trash", "tasks"]);
      } catch (error) {
        const placeholderKey = this.pairPlaceholderKey(folderName, pairIndex);
        if (this.deletedGalleryTimers[placeholderKey]) {
          window.clearTimeout(this.deletedGalleryTimers[placeholderKey]);
          delete this.deletedGalleryTimers[placeholderKey];
        }
        this.detail = previousDetail;
        if (previousCache) {
          this.cacheDetailPayload(folderName, previousCache);
        } else {
          this.invalidateDetailCache(folderName);
        }
        this.gallery = {
          ...this.gallery,
          items: previousGallery.items,
          total: previousGallery.total,
        };
        if (!this.viewer.open) {
          const fallbackSequence = (previousDetail.pairs || []).map((pair) => ({ pair, folder: previousDetail.folder }));
          const fallbackIndex = fallbackSequence.findIndex((entry) => Number(entry.pair?.pair_index) === Number(pairIndex));
          if (fallbackIndex >= 0) {
            this.viewerSource = "detail";
            this.viewerSequence = fallbackSequence;
            this.viewerIndex = fallbackIndex;
            this.openViewerEntry(fallbackSequence[fallbackIndex], true, false);
          }
        }
        this.notify("error", "删除失败", error.message || "后台删除失败，页面内容已恢复。");
      } finally {
        if (this.pairDeletingKey === key) {
          this.pairDeletingKey = null;
        }
      }
    },

    async createSubscription() {
      const uid = String(this.newSubscriptionUid || "").trim();
      if (!uid) {
        this.notify("error", "无法添加订阅", "请输入有效的 UID。");
        return;
      }
      const previousInput = this.newSubscriptionUid;
      const tempUid = `pending-${uid}`;
      this.newSubscriptionUid = "";
      this.subscriptions = [
        {
          uid: tempUid,
          uname: `正在添加 ${uid}`,
          status: "pending",
          pull_images: true,
          image_min_count: 6,
          pull_livephoto: true,
          include_forwarded: false,
          folder_count: 0,
          image_count: 0,
          livephoto_count: 0,
        },
        ...(this.subscriptions || []),
      ];
      this.notify("info", "正在添加订阅", `已开始添加 UID ${uid}。`);
      try {
        const result = await this.api("/api/subscriptions", {
          method: "POST",
          body: JSON.stringify({ uid }),
        });
        await Promise.all([this.loadSubscriptions(), this.refreshMeta()]);
        this.notify("success", "订阅已添加", result.message);
      } catch (error) {
        this.subscriptions = (this.subscriptions || []).filter((item) => item.uid !== tempUid);
        this.newSubscriptionUid = previousInput;
        this.notify("error", "添加订阅失败", error.message || "订阅已恢复到提交前状态。");
      }
    },

    async updateSubscriptionSetting(item, field, value) {
      const allowed = ["pull_images", "image_min_count", "pull_livephoto", "include_forwarded"];
      if (!allowed.includes(field)) {
        return;
      }
      const normalizeImageThreshold = (input, fallback = 6) => {
        const parsed = Number(input);
        if (!Number.isFinite(parsed)) {
          return fallback;
        }
        return Math.max(-1, Math.min(12, parsed));
      };
      const previous = {
        pull_images: !!item.pull_images,
        image_min_count: normalizeImageThreshold(item.image_min_count, 6),
        pull_livephoto: !!item.pull_livephoto,
        include_forwarded: !!item.include_forwarded,
      };
      if (field === "image_min_count") {
        item.image_min_count = normalizeImageThreshold(value, 6);
        item.pull_images = item.image_min_count >= 0;
      } else {
        item[field] = !!value;
      }
      try {
        const result = await this.api(`/api/subscriptions/${encodeURIComponent(item.uid)}`, {
          method: "PUT",
          body: JSON.stringify({
            pull_images: item.pull_images,
            image_min_count: item.image_min_count,
            pull_livephoto: item.pull_livephoto,
            include_forwarded: item.include_forwarded,
          }),
        });
        Object.assign(item, {
          ...result.item,
          pull_images: !!result.item.pull_images,
          image_min_count: normalizeImageThreshold(result.item.image_min_count, 6),
          pull_livephoto: !!result.item.pull_livephoto,
          include_forwarded: !!result.item.include_forwarded,
        });
        this.notify("success", "订阅策略已更新", result.message);
      } catch (error) {
        Object.assign(item, previous);
      }
    },

    async refreshSubscriptionProfile(uid) {
      const target = (this.subscriptions || []).find((item) => String(item.uid) === String(uid));
      const previousName = target?.uname;
      this.replaceSubscriptionLocally(uid, (item) => ({ ...item, uname: "正在刷新昵称..." }));
      try {
        const result = await this.api(`/api/subscriptions/${encodeURIComponent(uid)}/refresh-profile`, { method: "POST" });
        const refreshed = result.item || {};
        this.subscriptions = this.subscriptions.map((item) =>
          String(item.uid) === String(uid)
            ? {
                ...item,
                ...refreshed,
                icon_url: refreshed.icon_url || refreshed.avatar_url || item.icon_url || "",
                avatar_url: refreshed.avatar_url || item.avatar_url || "",
                pull_images: !!refreshed.pull_images,
                image_min_count: Number.isFinite(Number(refreshed.image_min_count)) ? Number(refreshed.image_min_count) : 6,
                pull_livephoto: !!refreshed.pull_livephoto,
                include_forwarded: !!refreshed.include_forwarded,
              }
            : item,
        );
        await this.refreshMeta();
        this.notify("success", "昵称已刷新", result.message);
      } catch (error) {
        this.replaceSubscriptionLocally(uid, (item) => ({ ...item, uname: previousName || item.uname || item.uid }));
        this.notify("error", "刷新昵称失败", error.message || "订阅名称已恢复。");
      }
    },

    async refreshSubscriptionIcon(uid) {
      const normalized = String(uid);
      if (!normalized || this.subscriptionIconRefreshingUid === normalized) {
        return;
      }
      if (normalized.startsWith("site:")) {
        this.subscriptionIconRefreshingUid = normalized;
        try {
          await this.refreshSiteSourceIcon({ id: normalized.replace(/^site:/, "") });
        } finally {
          this.subscriptionIconRefreshingUid = null;
        }
        return;
      }
      this.subscriptionIconRefreshingUid = normalized;
      this.iconLoadFailures = { ...this.iconLoadFailures, [normalized]: false };
      this.notify("info", "正在刷新订阅头像", "正在重新获取并下载该订阅的网站 icon。");
      try {
        const result = await this.api(`/api/subscriptions/${encodeURIComponent(normalized)}/refresh-icon`, { method: "POST" });
        const refreshed = result.item || {};
        this.subscriptions = (this.subscriptions || []).map((item) =>
          String(item.uid) === normalized
            ? this.normalizeSubscriptionItem({
                ...item,
                ...refreshed,
                icon_url: Object.prototype.hasOwnProperty.call(refreshed, "avatar_url")
                  ? (refreshed.icon_url || refreshed.avatar_url || "")
                  : (refreshed.icon_url || item.icon_url || ""),
                avatar_url: Object.prototype.hasOwnProperty.call(refreshed, "avatar_url")
                  ? (refreshed.avatar_url || "")
                  : (item.avatar_url || ""),
                avatar_tiny_url: Object.prototype.hasOwnProperty.call(refreshed, "avatar_tiny_url")
                  ? (refreshed.avatar_tiny_url || "")
                  : (item.avatar_tiny_url || ""),
                icon_tiny_url: Object.prototype.hasOwnProperty.call(refreshed, "avatar_tiny_url")
                  ? (refreshed.icon_tiny_url || refreshed.avatar_tiny_url || "")
                  : (refreshed.icon_tiny_url || item.icon_tiny_url || ""),
                image_min_count: Number.isFinite(Number(refreshed.image_min_count)) ? Number(refreshed.image_min_count) : item.image_min_count,
              })
            : item,
        );
        this.notify("success", "头像已刷新", result.message || "已更新订阅头像。");
      } catch (error) {
        this.notify("error", "头像刷新失败", error.message || "请稍后重试。");
      } finally {
        this.subscriptionIconRefreshingUid = null;
      }
    },

    jumpToSubscriptionSpace(uid) {
      const target = `https://space.bilibili.com/${encodeURIComponent(uid)}/dynamic`;
      window.open(target, "_blank", "noopener,noreferrer");
    },

    askSubscriptionReload(uid) {
      const normalized = String(uid);
      if (this.subscriptionReloadConfirmUid !== normalized) {
        this.subscriptionReloadConfirmUid = normalized;
        this.subscriptionReloadConfirmStep = 1;
        return;
      }
      this.subscriptionReloadConfirmStep = 2;
    },

    cancelSubscriptionReload() {
      this.subscriptionReloadConfirmUid = null;
      this.subscriptionReloadConfirmStep = 0;
    },

    async confirmSubscriptionReload(uid) {
      if (this.subscriptionReloadConfirmUid !== String(uid) || this.subscriptionReloadConfirmStep < 2) {
        return;
      }
      this.cancelSubscriptionReload();
      this.setImmediateTaskFeedback("正在提交当前订阅的全量校验拉取任务...");
      const result = await this.api(`/api/subscriptions/${encodeURIComponent(uid)}/reload`, { method: "POST" });
      await Promise.all([this.refreshStatus(), this.refreshTasks()]);
      this.notify("info", "任务已提交", result.message);
    },

    askSubscriptionDelete(uid) {
      const normalized = String(uid);
      if (this.subscriptionDeleteConfirmUid !== normalized) {
        this.subscriptionDeleteConfirmUid = normalized;
        this.subscriptionDeleteConfirmStep = 1;
        return;
      }
      this.subscriptionDeleteConfirmStep = Math.min(this.subscriptionDeleteConfirmStep + 1, 3);
    },

    cancelSubscriptionDelete() {
      this.subscriptionDeleteConfirmUid = null;
      this.subscriptionDeleteConfirmStep = 0;
    },

    async confirmSubscriptionDelete(uid) {
      if (this.subscriptionDeleteConfirmUid !== String(uid) || this.subscriptionDeleteConfirmStep < 3) {
        return;
      }
      this.cancelSubscriptionDelete();
      const previousSubscriptions = [...(this.subscriptions || [])];
      const previousSelected = [...(this.selectedSubscriptionUids || [])];
      this.selectedSubscriptionUids = this.selectedSubscriptionUids.filter((item) => item !== String(uid));
      this.subscriptions = (this.subscriptions || []).filter((item) => String(item.uid) !== String(uid));
      this.notify("info", "正在退订", "页面已先更新，后台正在删除相关内容。");
      try {
        const result = await this.api(`/api/subscriptions/${encodeURIComponent(uid)}`, { method: "DELETE" });
        await Promise.all([this.loadSubscriptions(), this.refreshMeta(), this.refreshGallery(true), this.refreshTrash()]);
        this.notify("success", "已退订", result.message);
      } catch (error) {
        this.subscriptions = previousSubscriptions;
        this.selectedSubscriptionUids = previousSelected;
        this.notify("error", "退订失败", error.message || "订阅列表已恢复。");
      }
    },

    subscriptionReloadLabel(uid) {
      if (this.subscriptionReloadConfirmUid !== String(uid)) {
        return "全量校验拉取";
      }
      return "再次确认";
    },

    subscriptionDeleteLabel(uid) {
      if (this.subscriptionDeleteConfirmUid !== String(uid)) {
        return "退订并删除相关内容";
      }
      return this.subscriptionDeleteConfirmStep === 1 ? "再次确认" : "最后确认";
    },

    async toggleSubscriptionStatus(uid) {
      const target = (this.subscriptions || []).find((item) => String(item.uid) === String(uid));
      const previousStatus = target?.status;
      this.replaceSubscriptionLocally(uid, (item) => ({
        ...item,
        status: item.status === "paused" ? "active" : "paused",
      }));
      try {
        const result = await this.api(`/api/subscriptions/${encodeURIComponent(uid)}/toggle`, { method: "POST" });
        await this.loadSubscriptions();
        this.notify("success", "订阅状态已更新", result.message);
      } catch (error) {
        this.replaceSubscriptionLocally(uid, (item) => ({ ...item, status: previousStatus || item.status }));
        this.notify("error", "订阅状态更新失败", error.message || "订阅状态已恢复。");
      }
    },

    applyFolderFavorite(folderName, favorite) {
      const folders = [...(this.gallery.items || []), ...(this.videoFeed.items || []), this.detail.folder, this.videoNote.folder];
      for (const folder of folders) {
        if (folder?.folder_name === folderName) folder.is_favorite = !!favorite;
      }
      this.updateCachedDetailFavorite(folderName, !!favorite);
    },

    async toggleFavorite(item, event = null) {
      event?.stopPropagation();
      if (!item?.folder_name || this.favoritePending[item.folder_name]) return;
      const folderName = item.folder_name;
      this.favoritePending[folderName] = true;
      const favorite = !item.is_favorite;
      const previous = !!item.is_favorite;
      item.is_favorite = favorite;
      this.applyFolderFavorite(folderName, favorite);
      if (this.category === "favorites" && !favorite) this.removeFolderFromGallery(folderName);
      try {
        const result = await this.api(`/api/gallery/folders/${encodeURIComponent(folderName)}/favorite`, {
          method: "POST",
          body: JSON.stringify({ favorite }),
        });
        item.is_favorite = result.favorite;
        this.applyFolderFavorite(folderName, result.favorite);
        await Promise.all([this.refreshMeta(), this.applyMutationSidebarCounts(result, ["all", "favorites", "livephoto"])]);
        this.notify("success", result.favorite ? "已收藏" : "已取消收藏", result.message);
      } catch (error) {
        item.is_favorite = previous;
        this.applyFolderFavorite(folderName, previous);
        await this.refreshGallery(true).catch(() => {});
        this.notify("error", "收藏失败", error.message || "请稍后重试");
      } finally {
        delete this.favoritePending[folderName];
      }
    },

    cardPrimaryText(item) {
      if (this.isSiteGalleryItem(item)) {
        return item.title || item.folder_name || "";
      }
      return item.text_prefix || item.title || item.folder_name || "";
    },

    cardSecondaryText(item) {
      if (this.isSiteGalleryItem(item)) {
        return this.truncateCardText(item.text_prefix || item.folder_name || "");
      }
      return item.title || item.folder_name || "";
    },

    isSiteGalleryItem(item) {
      return String(item?.subscription_uid || "").startsWith("site:");
    },

    truncateCardText(text) {
      const compact = String(text || "").trim();
      return compact.length > 20 ? `${compact.slice(0, 20)}…` : compact;
    },

    reviewItemRemoving(id) {
      return !!this.reviewRemovingIds[String(id)];
    },

    animateReviewItemRemoval(id) {
      const normalized = Number(id);
      const cards = Array.from(document.querySelectorAll(".review-card[data-review-id]"));
      const before = new Map(cards.map((card) => [card.getAttribute("data-review-id"), card.getBoundingClientRect().top]));
      this.reviewRemovingIds = { ...this.reviewRemovingIds, [String(normalized)]: true };
      return new Promise((resolve) => {
        window.setTimeout(() => {
          this.reviewItems = (this.reviewItems || []).filter((item) => Number(item.id) !== normalized);
          this.sidebarCounts = {
            ...this.sidebarCounts,
            review: Math.max(0, Number(this.sidebarCounts.review || 0) - 1),
          };
          this.$nextTick(() => {
            document.querySelectorAll(".review-card[data-review-id]").forEach((card) => {
              const previousTop = before.get(card.getAttribute("data-review-id"));
              if (!Number.isFinite(previousTop) || typeof card.animate !== "function") {
                return;
              }
              const delta = previousTop - card.getBoundingClientRect().top;
              if (Math.abs(delta) > 1) {
                card.animate(
                  [{ transform: `translate3d(0, ${delta}px, 0)` }, { transform: "translate3d(0, 0, 0)" }],
                  { duration: 260, easing: "cubic-bezier(0.16, 1, 0.3, 1)" },
                );
              }
            });
            const nextRemoving = { ...this.reviewRemovingIds };
            delete nextRemoving[String(normalized)];
            this.reviewRemovingIds = nextRemoving;
            resolve();
          });
        }, 180);
      });
    },

    async approveReview(id) {
      const normalized = Number(id);
      if (!normalized || this.reviewActionId) {
        return;
      }
      const previousItems = [...(this.reviewItems || [])];
      this.reviewActionId = normalized;
      this.closeSourcePreview();
      this.setImmediateTaskFeedback("正在提交待审核放行任务...");
      const removal = this.animateReviewItemRemoval(normalized);
      try {
        const [result] = await Promise.all([
          this.api(`/api/review/${normalized}/approve`, { method: "POST" }),
          removal,
        ]);
        await Promise.all([this.refreshReview(), this.refreshStatus(), this.refreshMeta()]);
        this.notify("success", "已开始处理", result.message);
      } catch (error) {
        await removal;
        this.reviewItems = previousItems;
        this.sidebarCounts = { ...this.sidebarCounts, review: previousItems.length };
        this.notify("error", "拉取提交失败", error.message || "待审核列表已恢复。");
      } finally {
        this.reviewActionId = null;
      }
    },

    queuedTaskCancelLabel(task) {
      return this.queuedCancelConfirmId === Number(task.queue_id) ? "再次确认撤销" : "撤销任务";
    },

    askCancelQueuedTask(queueId) {
      const normalized = Number(queueId);
      if (!normalized) {
        return;
      }
      if (this.queuedCancelConfirmId !== normalized) {
        this.queuedCancelConfirmId = normalized;
        return;
      }
      this.confirmCancelQueuedTask(normalized);
    },

    cancelQueuedTaskPrompt() {
      this.queuedCancelConfirmId = null;
    },

    async confirmCancelQueuedTask(queueId) {
      const normalized = Number(queueId);
      if (!normalized || this.queuedCancelConfirmId !== normalized) {
        return;
      }
      const previousQueue = [...(this.queuedTasks || [])];
      this.queuedCancelConfirmId = null;
      this.queuedTasks = (this.queuedTasks || []).filter((item) => Number(item.queue_id) !== normalized);
      this.notify("info", "正在撤销排队任务", "页面已先更新，后台正在处理。");
      try {
        const result = await this.api(`/api/tasks/queue/${normalized}/cancel`, { method: "POST" });
        await Promise.all([this.refreshTasks(), this.refreshReview(), this.refreshStatus()]);
        this.notify("success", "已撤销排队任务", result.message);
      } catch (error) {
        this.queuedTasks = previousQueue;
        this.notify("error", "撤销失败", error.message || "排队任务已恢复。");
      }
    },

    canRetryTask(item) {
      const action = item?.details?.retry_action;
      if (action?.kind === "thumbnail-rebuild" && !["small", "thumb"].includes(action.level)) {
        return false;
      }
      return (item?.status === "failed" || (["partial", "cancelled"].includes(item?.status)
        && ["xhs-sync", "xhs-verify", "xhs-authors", "livephoto-check", "livephoto-repair", "livephoto-repair-errors",
          "livephoto-manual"].includes(item?.task_type))) && !!item?.details?.retry_action?.kind;
    },

    async retryTask(item) {
      if (!this.canRetryTask(item)) {
        return;
      }
      this.notify("info", "正在重试任务", `任务 #${item.id} 已开始重新提交。`);
      const result = await this.api(`/api/tasks/${item.id}/retry`, { method: "POST" });
      await Promise.all([this.refreshTasks(), this.refreshStatus(), this.refreshReview()]);
      this.notify("info", "任务已重新提交", result.message);
    },

    reviewApproveLabel(item) {
      return item?.payload?.validation_mode ? "重新拉取" : "放行下载";
    },

    async rejectReview(id) {
      const normalized = Number(id);
      if (!normalized || this.reviewActionId) {
        return;
      }
      const previousItems = [...(this.reviewItems || [])];
      this.reviewActionId = normalized;
      this.closeSourcePreview();
      const removal = this.animateReviewItemRemoval(normalized);
      this.notify("info", "已提交删除", "");
      try {
        const [result] = await Promise.all([
          this.api(`/api/review/${normalized}/reject`, { method: "POST" }),
          removal,
        ]);
        await Promise.all([this.refreshReview(), this.refreshMeta(), this.refreshTrash()]);
        void result;
      } catch (error) {
        await removal;
        this.reviewItems = previousItems;
        this.sidebarCounts = { ...this.sidebarCounts, review: previousItems.length };
        this.notify("error", "忽略失败", error.message || "待审核列表已恢复。");
      } finally {
        this.reviewActionId = null;
      }
    },

    async refreshLogs() {
      const payload = await this.api("/api/filter/logs");
      this.logs = payload.items;
      this.lazyLoaded.logs = true;
      this.sidebarCounts = { ...this.sidebarCounts, logs: this.logs.length };
    },

    clearFilterLogsButtonLabel() {
      return this.clearFilterLogsConfirmStep === 0 ? "清理已结束日志" : "再次确认清理";
    },

    askClearFilterLogs() {
      this.clearFilterLogsConfirmStep = Math.min(this.clearFilterLogsConfirmStep + 1, 1);
    },

    cancelClearFilterLogs() {
      this.clearFilterLogsConfirmStep = 0;
    },

    async confirmClearFilterLogs() {
      if (this.clearFilterLogsConfirmStep < 1) {
        return;
      }
      const previousLogs = [...(this.logs || [])];
      this.logs = [];
      this.clearFilterLogsConfirmStep = 0;
      this.notify("info", "正在清理过滤日志", "页面已先更新，后台正在处理。");
      try {
        const result = await this.api("/api/filter/logs/clear", { method: "POST" });
        await this.refreshLogs();
        this.notify("success", "过滤日志已清理", result.message);
      } catch (error) {
        this.logs = previousLogs;
        this.notify("error", "清理失败", error.message || "过滤日志已恢复。");
      }
    },

    async loadSettings() {
      if (this.settingsDirty()) return;
      const settings = await this.api("/api/settings");
      this.settings = settings;
      this.savedGalleryDefaultRandom = settings.gallery_default_random === true;
      this.settingsError = settings.intranet_config_error || "";
      document.title = settings.app_title || document.title;
      this.keywordText = (settings.ad_filter_keywords || []).join("\n");
      this.syncThemeState(settings);
      this.settingsBaseline = this.settingsFingerprint();
      this.lazyLoaded.settings = true;
      // Secondary maintenance statistics never hold the settings form hostage.
      await Promise.allSettled([
        this.api("/api/health").then(health => { this.galleryIndexStatus = health.gallery_index || {}; }),
        this.api("/api/settings/storage-stats").then(result => { this.storageStats = result.stats || {}; }),
        this.api("/api/settings/theme").then(spec => this.applyThemeSpec(spec)),
        this.checkDataUpgrade(),
      ]);
    },

    async checkDataUpgrade() {
      this.dataUpgrade = await this.api('/api/settings/data-upgrade');
    },

    async startDataUpgrade(action = 'upgrade') {
      if (this.dataUpgradeSubmitting) return;
      this.dataUpgradeSubmitting = true;
      try {
        await this.api('/api/settings/data-upgrade/start', { method: 'POST', body: JSON.stringify({ action }) });
        window.location.reload();
      } catch (error) {
        this.notify('error', '无法开始数据维护', error.message);
      } finally { this.dataUpgradeSubmitting = false; }
    },

    async loadDataCheckReport() {
      try {
        this.dataCheckReport = await this.api('/api/settings/data-upgrade/report');
        this.dataCheckReportPage = 1;
      } catch (error) { this.notify('error', '读取检查明细失败', error.message); }
    },

    dataCheckReportRows() {
      return (this.dataCheckReport?.items || []).slice((this.dataCheckReportPage - 1) * 50, this.dataCheckReportPage * 50);
    },

    formatBytes(value) {
      const bytes = Math.max(0, Number(value) || 0);
      if (bytes < 1024) {
        return `${bytes} B`;
      }
      const units = ["KB", "MB", "GB", "TB"];
      let size = bytes / 1024;
      let index = 0;
      while (size >= 1024 && index < units.length - 1) {
        size /= 1024;
        index += 1;
      }
      return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${units[index]}`;
    },

    storageStatsUpdatedText() {
      if (!this.storageStats?.updated_at) {
        return "尚未统计";
      }
      return this.formatDateTime(this.storageStats.updated_at);
    },

    formatDateTime(value) {
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) {
        return String(value || "");
      }
      return date.toLocaleString("zh-CN", { hour12: false });
    },

    formatDateYMD(value) {
      const date = this.parseDateValue(value);
      if (!date) {
        return "";
      }
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${year}/${month}/${day}`;
    },

    parseDateValue(value) {
      if (value === null || value === undefined || value === "") {
        return null;
      }
      if (typeof value === "number" && Number.isFinite(value)) {
        if (value <= 0) {
          return null;
        }
        const timestamp = value > 100000000000 ? value : value * 1000;
        const date = new Date(timestamp);
        return Number.isNaN(date.getTime()) ? null : date;
      }
      const raw = String(value).trim();
      if (!raw) {
        return null;
      }
      if (/^\d+$/.test(raw)) {
        return this.parseDateValue(Number(raw));
      }
      if (/[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$/.test(raw)) {
        const zoned = new Date(raw.replace(" ", "T"));
        return Number.isNaN(zoned.getTime()) ? null : zoned;
      }
      const match = raw.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?/);
      if (match) {
        const date = new Date(
          Number(match[1]),
          Number(match[2]) - 1,
          Number(match[3]),
          Number(match[4] || 0),
          Number(match[5] || 0),
          Number(match[6] || 0),
        );
        return Number.isNaN(date.getTime()) ? null : date;
      }
      const date = new Date(raw);
      return Number.isNaN(date.getTime()) ? null : date;
    },

    async refreshStorageStats(force = false) {
      if (this.storageStatsLoading) {
        return;
      }
      this.storageStatsLoading = true;
      try {
        const endpoint = force ? "/api/settings/storage-stats/refresh" : "/api/settings/storage-stats";
        const result = await this.api(endpoint, { method: force ? "POST" : "GET" });
        this.storageStats = result.stats || {};
      } finally {
        this.storageStatsLoading = false;
      }
    },

    async cleanupStorageTrash() {
      if (this.storageCleanupRunning) {
        return;
      }
      this.storageCleanupRunning = true;
      this.setImmediateTaskFeedback("正在提交垃圾文件清理任务...");
      try {
        const result = await this.api("/api/settings/storage-cleanup", { method: "POST" });
        await Promise.all([this.refreshStatus(), this.refreshTasks()]);
        this.notify("info", "垃圾清理已提交", result.message || "后台正在清理垃圾文件。");
      } catch (error) {
        this.notify("error", "垃圾清理提交失败", error.message || "请稍后重试。");
      } finally {
        this.storageCleanupRunning = false;
      }
    },

    async saveSettings() {
      if (this.settingsSaving) return;
      this.settingsError = "";
      const invalid = [...document.querySelectorAll('.settings-pane input')].find((input) => !input.checkValidity());
      if (invalid) {
        this.settingsError = `${invalid.closest('label')?.textContent?.trim() || '字段'}：请输入有效范围内的数值`;
        this.settingsGroup = invalid.closest('[data-settings-group]')?.dataset.settingsGroup || this.settingsGroup;
        this.$nextTick(() => invalid.reportValidity());
        return;
      }
      this.settingsSaving = true;
      try {
        const { auth, intranet_config_error, tiny_thumb_edge, tiny_thumb_quality, thumb_edge, thumb_quality, ...settingsPayload } = this.settings || {};
        const payload = {
          ...settingsPayload,
          wide_single_cover: Boolean(settingsPayload.wide_single_cover),
          random_cover_enabled: settingsPayload.random_cover_enabled !== false,
          gallery_default_random: settingsPayload.gallery_default_random === true,
          scheduler_enabled: Boolean(settingsPayload.scheduler_enabled),
          scheduler_interval_hours: Math.max(1, Number(settingsPayload.scheduler_interval_hours) || 12),
          site_scheduler_enabled: Boolean(settingsPayload.site_scheduler_enabled),
          site_scheduler_interval_hours: Math.max(1, Number(settingsPayload.site_scheduler_interval_hours) || 12),
          site_request_timeout: Math.max(30, Math.min(900, Number(settingsPayload.site_request_timeout) || 300)),
          site_request_sleep: Math.max(0, Number(settingsPayload.site_request_sleep) || 0),
          site_max_media_per_post: Math.max(1, Math.min(500, Number(settingsPayload.site_max_media_per_post) || 100)),
          site_proxy_enabled: Boolean(settingsPayload.site_proxy_enabled),
          site_proxy_host: String(settingsPayload.site_proxy_host || "127.0.0.1").trim() || "127.0.0.1",
          site_proxy_port: Math.max(1, Math.min(65535, Number(settingsPayload.site_proxy_port) || 7890)),
          review_source_open_mode: settingsPayload.review_source_open_mode === "popup" ? "popup" : "browser",
          duplicate_image_threshold: Math.max(1, Math.min(100, Number(settingsPayload.duplicate_image_threshold) || 2)),
          app_title: String(settingsPayload.app_title || "RC-WebGallery").trim() || "RC-WebGallery",
          video_default_loop: settingsPayload.video_default_loop !== false,
          video_default_volume: Math.max(0, Math.min(100, Math.round(Number(settingsPayload.video_default_volume) || 0))),
          small_thumb_edge: Math.max(64, Math.min(1024, Number(settingsPayload.small_thumb_edge) || 368)),
          small_thumb_quality: Math.max(1, Math.min(100, Number(settingsPayload.small_thumb_quality) || 50)),
          ad_filter_keywords: this.keywordText
            .split("\n")
            .map((item) => item.trim())
            .filter(Boolean),
        };
        this.notify("info", "正在保存设置", "前端已保留当前改动，后台正在写入。");
        const savedSettings = await this.api("/api/settings", {
          method: "PUT",
          body: JSON.stringify(payload),
        });
        this.settings = { ...savedSettings, auth: auth || this.settings.auth };
        this.savedGalleryDefaultRandom = this.settings.gallery_default_random === true;
        this.keywordText = (this.settings.ad_filter_keywords || []).join("\n");
        this.settingsBaseline = this.settingsFingerprint();
        document.title = this.settings.app_title || document.title;
        await Promise.all([this.refreshMeta(), this.loadSettings()]);
        this.scheduleIdleWork(() => this.refreshDuplicates().catch(() => {}), 500);
        this.notify("success", "设置已保存", "配置已写入，内网跳转将在下次打开或刷新页面时生效。");
      } catch (error) {
        this.settingsError = error.message || "保存失败，当前改动仍然保留，请重试";
        if (this.settingsError.includes("内网") || this.settingsError.includes("config.json")) this.settingsGroup = "network";
        this.notify("error", "设置未保存", this.settingsError);
      } finally {
        this.settingsSaving = false;
      }
    },

    validateButtonLabel() {
      return this.validateConfirmStep === 0 ? "校验当前内容" : "再次确认校验";
    },

    revealValidateActions() {
      this.validateActionsRevealed = true;
    },

    askValidateContent() {
      this.validateConfirmStep = Math.min(this.validateConfirmStep + 1, 2);
    },

    cancelValidateContent() {
      this.validateConfirmStep = 0;
      this.validateActionsRevealed = false;
    },

    async confirmValidateContent() {
      if (this.validateConfirmStep < 1) {
        return;
      }
      this.setImmediateTaskFeedback("正在提交内容校验任务...");
      const result = await this.api("/api/settings/validate-content", { method: "POST" });
      this.validateConfirmStep = 0;
      // 校验可能整条删除补不齐的贴文，提交后重新收起，避免被连续误点。
      this.validateActionsRevealed = false;
      await this.refreshStatus();
      await this.refreshTasks();
      this.notify("info", "校验任务已提交", result.message);
    },

    // 危险操作总抽屉：收起时连同内部抽屉一起合上，并丢掉未完成的两步/三步确认，
    // 避免下次展开直接落在「确认清空」这类高危按钮上。
    collapseDangerOperations(element) {
      if (!element || element.open) {
        return;
      }
      for (const inner of element.querySelectorAll("details")) {
        inner.open = false;
      }
      this.cancelValidateContent();
      this.clearDataConfirmStep = 0;
    },

    rebuildIndexButtonLabel() {
      return this.rebuildIndexConfirmStep === 0 ? "重建页面索引" : "再次确认重建";
    },

    livephotoTaskKindSet() {
      return new Set(["livephoto-check", "livephoto-repair", "livephoto-repair-errors", "livephoto-manual"]);
    },

    livephotoTaskActive() {
      const kinds = this.livephotoTaskKindSet();
      if ((this.pullStatus?.queue || []).some((item) => kinds.has(item.kind))) return true;
      if ((this.queuedTasks || []).some((item) => kinds.has(item.kind))) return true;
      if ((this.taskRuns || []).some((item) => kinds.has(item.task_type) && item.status === "running")) return true;
      return kinds.has(String(this.pullStatus?.mode || ""));
    },

    livephotoLatestResult() {
      const kinds = this.livephotoTaskKindSet();
      const task = (this.taskRuns || []).find((item) => kinds.has(item.task_type)
        && ["success", "partial"].includes(item.status)
        && item.details
        && item.details.completed !== false);
      if (!task) return null;
      const details = task.details;
      const exceptionTargets = Array.isArray(details.exception_targets)
        ? details.exception_targets
        : (details.issues || []).filter((issue) => issue?.code && issue.code !== "metadata_error");
      return {
        id: task.id,
        label: this.taskModeLabel(task.task_type) || "实况配对检查",
        finishedAt: task.finished_at || "",
        subset: details.scan_scope === "subset" || Array.isArray(details.scope_folders)
          || (details.scan_scope !== "all" && !("scope_folders" in details) && !!details.source_task_id),
        checked: Number(details.checked) || 0,
        paired: Number(details.paired) || 0,
        repairable: Number(details.repairable) || (details.repairable_targets || []).length || 0,
        exceptions: exceptionTargets.length || Number(details.unpaired) || 0,
      };
    },

    livephotoRepairVisible() {
      const result = this.livephotoLatestResult();
      return !this.livephotoTaskActive() && !!result && result.repairable > 0;
    },

    livephotoExceptionVisible() {
      const result = this.livephotoLatestResult();
      return !this.livephotoTaskActive() && !!result && result.repairable === 0 && result.exceptions > 0;
    },

    livephotoResultText() {
      const result = this.livephotoLatestResult();
      if (!result) return "";
      const time = this.formatDateTime(result.finishedAt);
      const parts = [`检查 ${result.checked} 组`, `已配对 ${result.paired} 组`];
      if (result.repairable) parts.push(`待修复配对 ${result.repairable} 组`);
      if (result.exceptions) parts.push(`异常 ${result.exceptions} 组`);
      return `${result.label}（${result.subset ? "局部复查" : "全库检查"}）：${parts.join(" · ")}${time ? `（${time}）` : ""}`;
    },

    xhsAuthorTaskActive() {
      return this.pullStatus?.mode === "xhs-authors"
        || (this.pullStatus?.queue || []).some((item) => item.kind === "xhs-authors")
        || (this.queuedTasks || []).some((item) => item.kind === "xhs-authors")
        || (this.taskRuns || []).some((item) => item.task_type === "xhs-authors" && ["queued", "running", "paused"].includes(item.status));
    },

    xhsAuthorLatestTask() {
      return (this.taskRuns || []).find((item) => item.task_type === "xhs-authors");
    },

    async submitXhsAuthorCheck() {
      if (this.xhsAuthorSubmitting || this.xhsAuthorTaskActive()) return;
      this.xhsAuthorSubmitting = true;
      try {
        const result = await this.api("/api/settings/xhs/authors/check", { method: "POST" });
        await Promise.all([this.refreshStatus(), this.refreshTasks()]);
        this.notify("info", "作者校验已提交", result.message);
      } catch (error) {
        this.notify("error", "作者校验提交失败", error.message);
      } finally {
        this.xhsAuthorSubmitting = false;
      }
    },

    async submitLivephotoCheck() {
      if (this.livephotoSubmitting) return;
      this.livephotoSubmitting = true;
      try {
        const result = await this.api("/api/settings/livephoto/check", { method: "POST" });
        await Promise.all([this.refreshStatus(), this.refreshTasks()]);
        this.notify("info", "实况检查已提交", result.message);
      } catch (error) {
        this.notify("error", "实况任务提交失败", error.message);
      } finally {
        this.livephotoSubmitting = false;
      }
    },

    async submitLivephotoFollowup(action) {
      const source = this.livephotoLatestResult();
      if (!source || this.livephotoSubmitting) return;
      this.livephotoSubmitting = true;
      try {
        const result = await this.api(`/api/settings/livephoto/${source.id}/followup/${action}`, { method: "POST" });
        await Promise.all([this.refreshStatus(), this.refreshTasks()]);
        this.notify("info", action === "repair" ? "修复配对已提交" : "修复异常已提交", result.message);
      } catch (error) {
        this.notify("error", "实况修复任务提交失败", error.message);
      } finally {
        this.livephotoSubmitting = false;
      }
    },

    askRebuildGalleryIndex() {
      this.rebuildIndexConfirmStep = Math.min(this.rebuildIndexConfirmStep + 1, 2);
    },

    cancelRebuildGalleryIndex() {
      this.rebuildIndexConfirmStep = 0;
    },

    async confirmRebuildGalleryIndex() {
      if (this.rebuildIndexConfirmStep < 1) {
        return;
      }
      this.setImmediateTaskFeedback("正在提交页面索引重建任务...");
      const result = await this.api("/api/settings/rebuild-gallery-index", { method: "POST" });
      this.rebuildIndexConfirmStep = 0;
      await Promise.all([this.refreshStatus(), this.refreshTasks(), this.loadSettings()]);
      this.notify("info", "索引任务已提交", result.message);
    },

    askThumbnailRebuild(level) {
      this.thumbnailRebuildConfirmLevel = level;
    },

    cancelThumbnailRebuild() {
      this.thumbnailRebuildConfirmLevel = null;
    },

    async confirmThumbnailRebuild(level) {
      if (this.thumbnailRebuildConfirmLevel !== level) {
        return;
      }
      this.setImmediateTaskFeedback(`正在提交 ${level} 缩略图重建任务...`);
      const result = await this.api(`/api/settings/rebuild-thumbnails/${encodeURIComponent(level)}`, { method: "POST" });
      this.thumbnailRebuildConfirmLevel = null;
      await Promise.all([this.refreshStatus(), this.refreshTasks(), this.loadSettings()]);
      this.notify("info", "缩略图任务已提交", result.message);
    },

    askResetSubscriptionIcons() {
      if (!this.resetSubscriptionIconsRunning) {
        this.resetSubscriptionIconsConfirmStep = 1;
      }
    },

    cancelResetSubscriptionIcons() {
      if (!this.resetSubscriptionIconsRunning) {
        this.resetSubscriptionIconsConfirmStep = 0;
      }
    },

    async confirmResetSubscriptionIcons() {
      if (this.resetSubscriptionIconsConfirmStep < 1 || this.resetSubscriptionIconsRunning) {
        return;
      }
      this.resetSubscriptionIconsRunning = true;
      this.resetSubscriptionIconsConfirmStep = 0;
      this.notify("info", "正在提交动态图标重置", "后台会逐个重新获取动态订阅头像。");
      try {
        const result = await this.api("/api/settings/reset-icons/subscriptions", { method: "POST" });
        this.iconLoadFailures = {};
        await Promise.all([this.refreshStatus(), this.refreshTasks()]);
        this.notify("success", "动态图标任务已提交", result.message || "后台正在刷新动态订阅头像。");
      } catch (error) {
        this.notify("error", "动态图标重置提交失败", error.message || "请稍后重试。");
      } finally {
        this.resetSubscriptionIconsRunning = false;
      }
    },

    askResetSiteIcons() {
      if (!this.resetSiteIconsRunning) {
        this.resetSiteIconsConfirmStep = 1;
      }
    },

    cancelResetSiteIcons() {
      if (!this.resetSiteIconsRunning) {
        this.resetSiteIconsConfirmStep = 0;
      }
    },

    async confirmResetSiteIcons() {
      if (this.resetSiteIconsConfirmStep < 1 || this.resetSiteIconsRunning) {
        return;
      }
      this.resetSiteIconsRunning = true;
      this.resetSiteIconsConfirmStep = 0;
      this.notify("info", "正在提交站点图标重置", "后台会逐个重新获取站点订阅图标。");
      try {
        const result = await this.api("/api/settings/reset-icons/sites", { method: "POST" });
        this.iconLoadFailures = {};
        await Promise.all([this.refreshStatus(), this.refreshTasks()]);
        this.notify("success", "站点图标任务已提交", result.message || "后台正在刷新站点订阅图标。");
      } catch (error) {
        this.notify("error", "站点图标重置提交失败", error.message || "请稍后重试。");
      } finally {
        this.resetSiteIconsRunning = false;
      }
    },

    askResetXhsIcons() {
      if (!this.resetXhsIconsRunning) {
        this.resetXhsIconsConfirmStep = 1;
      }
    },

    cancelResetXhsIcons() {
      if (!this.resetXhsIconsRunning) {
        this.resetXhsIconsConfirmStep = 0;
      }
    },

    async confirmResetXhsIcons() {
      if (this.resetXhsIconsConfirmStep < 1 || this.resetXhsIconsRunning) {
        return;
      }
      this.resetXhsIconsRunning = true;
      this.resetXhsIconsConfirmStep = 0;
      this.notify("info", "正在提交小红书图标重置", "后台会重新检查小红书登录状态并刷新图标。");
      try {
        const result = await this.api("/api/settings/reset-icons/xhs", { method: "POST" });
        this.iconLoadFailures = {};
        await Promise.all([this.refreshStatus(), this.refreshTasks()]);
        this.notify("success", "小红书图标任务已提交", result.message || "后台正在刷新小红书订阅图标。");
      } catch (error) {
        this.notify("error", "小红书图标重置提交失败", error.message || "请稍后重试。");
      } finally {
        this.resetXhsIconsRunning = false;
      }
    },

    // 界面配色：滑杆只改本地草稿，服务端派生才是最终结果，因此预览用同一套算法在前端复刻。
    applyThemeSpec(spec) {
      if (!spec || typeof spec !== "object") {
        return;
      }
      if (spec.defaults && typeof spec.defaults === "object") {
        this.themeDefaults = UI_THEME_ENGINE.normalize(spec.defaults);
      }
      if (spec.bounds && typeof spec.bounds === "object") {
        for (const key of ["hue", "saturation", "value"]) {
          const bound = spec.bounds[key];
          if (Array.isArray(bound) && bound.length === 2 && bound.every((item) => Number.isFinite(Number(item)))) {
            this.themeBounds = { ...this.themeBounds, [key]: [Number(bound[0]), Number(bound[1])] };
          }
        }
      }
    },

    syncThemeState(settings) {
      const saved = UI_THEME_ENGINE.normalize(settings?.ui_theme);
      const stamp = UI_THEME_ENGINE.revision(saved);
      if (stamp === this.themeSavedRevision && Object.keys(this.themeSwatches || {}).length) {
        // 已同步过：保留用户正在调整的草稿，别让别处的保存动作把它冲掉。
        return;
      }
      this.themeSaved = saved;
      this.themeSavedRevision = stamp;
      this.themeDraft = { ...saved };
      this.refreshThemeSwatches();
    },

    refreshThemeSwatches() {
      const theme = UI_THEME_ENGINE.normalize(this.themeDraft);
      this.themeDraft = theme;
      const tint = (key) => UI_THEME_ENGINE.transformHex(UI_THEME_BASE_COLORS[key], theme);
      const accent = tint("accent");
      this.themeSwatches = {
        text: tint("text"),
        muted: tint("muted"),
        accent,
        accentSoft: UI_THEME_ENGINE.rgba(accent, 0.12),
        accent2: tint("accent2"),
        accentWarm: tint("accentWarm"),
        danger: tint("danger"),
        line: UI_THEME_ENGINE.rgba(tint("line"), 0.14),
        lineStrong: UI_THEME_ENGINE.rgba(tint("line"), 0.24),
        surface: tint("surface"),
        page: tint("page"),
        onAccent: "#ffffff",
      };
      this.themeContrastRatio = UI_THEME_ENGINE.contrast("#ffffff", accent);
    },

    themeSwatchList() {
      const entries = [
        ["主色", "accent"],
        ["强调色", "accent2"],
        ["暖色", "accentWarm"],
        ["警示", "danger"],
        ["正文", "text"],
        ["次要文字", "muted"],
        ["页面底色", "page"],
        ["卡片底色", "surface"],
      ];
      return entries.map(([label, key]) => ({ label, color: this.themeSwatches[key] || "#000000" }));
    },

    themePickerHex() {
      // 取色器只承担「选主色」这一件事：固定饱和度与明度，呈现该色相下的典型主色。
      return UI_THEME_ENGINE.rgbToHex(UI_THEME_ENGINE.hsvToRgb(this.themeDraft.hue, 0.62, 0.72));
    },

    setThemeHueFromHex(hex) {
      const rgb = UI_THEME_ENGINE.hexToRgb(hex);
      if (!rgb) {
        return;
      }
      const [hue] = UI_THEME_ENGINE.rgbToHsv(rgb);
      this.themeDraft = { ...this.themeDraft, hue: Math.round(hue * 10) / 10 };
      this.refreshThemeSwatches();
    },

    onThemeInput() {
      this.refreshThemeSwatches();
    },

    setThemeNumber(key, raw) {
      const value = Number(raw);
      if (!Number.isFinite(value)) {
        return;
      }
      this.themeDraft = { ...this.themeDraft, [key]: value };
      this.refreshThemeSwatches();
    },

    themeParamText(key) {
      const value = Number(this.themeDraft[key] || 0);
      if (key === "hue") {
        return `${Math.round(value)}°`;
      }
      return `${Math.round(value * 100)}%`;
    },

    themeDraftChanged() {
      return UI_THEME_ENGINE.revision(this.themeDraft) !== this.themeSavedRevision;
    },

    themeHintText() {
      if (this.themeDraftChanged()) {
        return "有未保存的配色调整，点击「保存配色」后全站生效。";
      }
      const ratio = Number(this.themeContrastRatio || 0).toFixed(2);
      return `白字压在主色上的对比度 ${ratio}:1，满足正文可读性要求。`;
    },

    themeBound(key, index) {
      const bound = this.themeBounds[key] || UI_THEME_BOUNDS[key];
      return bound[index];
    },

    async saveTheme() {
      if (this.themeSaving || !this.themeDraftChanged()) {
        return;
      }
      this.themeSaving = true;
      try {
        const result = await this.api("/api/settings/theme", {
          method: "POST",
          body: JSON.stringify(UI_THEME_ENGINE.normalize(this.themeDraft)),
        });
        const saved = UI_THEME_ENGINE.normalize(result?.theme);
        this.themeSaved = saved;
        this.themeSavedRevision = UI_THEME_ENGINE.revision(saved);
        this.themeDraft = { ...saved };
        this.notify("success", "配色已保存", "界面配色已写入，页面即将刷新以套用新色板。");
        setTimeout(() => window.location.reload(), 400);
      } catch (error) {
        this.notify("error", "配色保存失败", error.message || "请稍后重试。");
        this.themeSaving = false;
      }
    },

    askResetTheme() {
      if (!this.themeSaving) {
        this.themeResetConfirmStep = 1;
      }
    },

    cancelResetTheme() {
      if (!this.themeSaving) {
        this.themeResetConfirmStep = 0;
      }
    },

    async confirmResetTheme() {
      if (this.themeResetConfirmStep < 1 || this.themeSaving) {
        return;
      }
      this.themeSaving = true;
      this.themeResetConfirmStep = 0;
      try {
        const result = await this.api("/api/settings/theme/reset", { method: "POST" });
        const saved = UI_THEME_ENGINE.normalize(result?.theme);
        this.themeSaved = saved;
        this.themeSavedRevision = UI_THEME_ENGINE.revision(saved);
        this.themeDraft = { ...saved };
        this.refreshThemeSwatches();
        this.notify("success", "配色已恢复默认", "界面配色已重置为默认方案，页面即将刷新。");
        setTimeout(() => window.location.reload(), 400);
      } catch (error) {
        this.notify("error", "配色重置失败", error.message || "请稍后重试。");
        this.themeSaving = false;
      }
    },

    fullReloadButtonLabel() {
      if (this.fullReloadConfirmStep === 0) {
        return "全量拉取当前所有动态";
      }
      if (this.fullReloadConfirmStep === 1) {
        return "再次确认全量拉取";
      }
      return "最后确认并开始全量拉取";
    },

    askFullReload() {
      this.fullReloadConfirmStep = Math.min(this.fullReloadConfirmStep + 1, 3);
    },

    cancelFullReload() {
      this.fullReloadConfirmStep = 0;
    },

    async confirmFullReload() {
      if (this.fullReloadConfirmStep < 3) {
        return;
      }
      this.setImmediateTaskFeedback("正在提交全量拉取任务...");
      const result = await this.api("/api/pull/reload-all", { method: "POST" });
      this.fullReloadConfirmStep = 0;
      await Promise.all([this.refreshStatus(), this.refreshTasks()]);
      this.notify("info", "已提交全量拉取", result.message);
    },

    clearDataButtonLabel() {
      if (this.clearDataConfirmStep === 0) {
        return "清空所有内容数据";
      }
      if (this.clearDataConfirmStep === 1) {
        return "再次确认清空";
      }
      return "最后确认并清空";
    },

    askClearAllData() {
      this.clearDataConfirmStep = Math.min(this.clearDataConfirmStep + 1, 3);
    },

    cancelClearAllData() {
      this.clearDataConfirmStep = 0;
    },

    async confirmClearAllData() {
      if (this.clearDataConfirmStep < 3) {
        return;
      }
      const previousState = {
        gallery: {
          ...this.gallery,
          items: [...(this.gallery.items || [])],
        },
        reviewItems: [...(this.reviewItems || [])],
        logs: [...(this.logs || [])],
        taskRuns: [...(this.taskRuns || [])],
        queuedTasks: [...(this.queuedTasks || [])],
        trashItems: [...(this.trashItems || [])],
        recentUpdates: this.cloneRecentUpdates(),
        recentUpdateDisclosure: this.cloneRecentUpdateDisclosure(),
      };
      this.clearDataConfirmStep = 0;
      this.closeGalleryLayers();
      this.clearLocalGalleryCaches();
      this.gallery = { ...this.gallery, items: [], total: 0, page: 1 };
      this.reviewItems = [];
      this.logs = [];
      this.taskRuns = [];
      this.queuedTasks = [];
      this.trashItems = [];
      this.resetRecentUpdateDisclosure();
      this.recentUpdates = { groups: [], total_groups: 0, total_items: 0 };
      this.notify("info", "正在清空全部内容", "页面已先清空，后台正在处理。");
      try {
        const result = await this.api("/api/settings/clear-data", { method: "POST" });
        await Promise.all([
          this.refreshMeta(),
          this.refreshGallery(true),
          this.refreshReview(),
          this.refreshLogs(),
          this.refreshTasks(),
          this.refreshTrash(),
          this.refreshStatus(),
        ]);
        this.notify("success", "内容已清空", result.message);
      } catch (error) {
        this.gallery = previousState.gallery;
        this.reviewItems = previousState.reviewItems;
        this.logs = previousState.logs;
        this.taskRuns = previousState.taskRuns;
        this.queuedTasks = previousState.queuedTasks;
        this.trashItems = previousState.trashItems;
        this.recentUpdates = previousState.recentUpdates;
        this.restoreRecentUpdateDisclosure(previousState.recentUpdateDisclosure);
        this.notify("error", "清空失败", error.message || "页面内容已恢复。");
      }
    },

    async pullAllSubscriptions() {
      if (this.currentView !== 'subscriptions' || this.allSubscriptionsPullPending) return;
      this.allSubscriptionsPullPending = true;
      try {
        const sources = [
          ['UP 主动态', '/api/pull/run'],
          ['站点订阅', '/api/site-sync'],
          ['小红书', '/api/xhs/sync'],
        ];
        const results = await Promise.allSettled(sources.map(([, url]) => this.api(url, { method: 'POST', body: '{}' })));
        const submitted = [], failed = [];
        results.forEach((result, index) => {
          const name = sources[index][0];
          if (result.status === 'fulfilled') submitted.push(name);
          else failed.push(`${name}：${result.reason?.message || '提交失败'}`);
        });
        if (submitted.length) this.clearLocalGalleryCaches();
        this.scheduleStatusRefresh();
        await Promise.allSettled([this.refreshStatus(), this.refreshTasks()]);
        this.notify(submitted.length ? (failed.length ? 'info' : 'success') : 'error',
          submitted.length ? (failed.length ? '部分订阅拉取已提交' : '全部订阅拉取已提交') : '拉取未能提交',
          [submitted.length ? `已提交：${submitted.join('、')}` : '', ...failed].filter(Boolean).join('；'));
      } finally {
        this.allSubscriptionsPullPending = false;
      }
    },

    async runPull() {
      if (this.currentView !== 'subscriptions' || this.subscriptionPanel !== 'up' || this.subscriptionPullPending) return;
      this.subscriptionPullPending = true;
      try {
        this.setImmediateTaskFeedback('正在提交 UP 主动态拉取任务...');
        const result = await this.api('/api/pull/run', { method: 'POST' });
        this.clearLocalGalleryCaches();
        this.scheduleStatusRefresh();
        await Promise.all([this.refreshStatus(), this.refreshTasks(), this.applyMutationSidebarCounts(result, ['all', 'video', 'livephoto', 'tasks'])]);
        this.notify('info', 'UP 主拉取已提交', result.message);
      } catch (error) {
        this.notify('error', '拉取失败', error.message || '请稍后重试');
      } finally {
        this.subscriptionPullPending = false;
      }
    },

    async refreshStatus() {
      const previousRunning = this.lastRunning;
      const previousTaskId = this.lastTaskId;
      const previousSiteRunning = this.lastSiteRunning;
      const previousSiteTaskId = this.lastSiteTaskId;
      const health = await this.api("/api/health");
      this.pullStatus = health.status || {};
      this.siteStatus = health.site_status || {};
      this.siteStats = health.site_stats || {};
      this.galleryIndexStatus = health.gallery_index || this.galleryIndexStatus || {};
      if (health.sidebar_counts) {
        this.applySidebarCounts(health.sidebar_counts);
      }
      const currentTaskId = this.pullStatus.last_run?.id || null;
      const currentSiteTaskId = this.siteStatus.last_run?.id || null;
      this.lastRunning = !!this.pullStatus.running;
      this.lastTaskId = currentTaskId;
      this.lastSiteRunning = !!this.siteStatus.running;
      this.lastSiteTaskId = currentSiteTaskId;
      if (currentTaskId && currentTaskId !== previousTaskId && !this.pullStatus.running) {
        await Promise.all([
          this.refreshMeta(),
          this.loadSubscriptions(),
          this.lazyLoaded.review || this.currentView === "review" ? this.refreshReview() : Promise.resolve(),
          this.lazyLoaded.duplicates || this.currentView === "duplicates" ? this.refreshDuplicates() : Promise.resolve(),
          this.lazyLoaded.logs || this.currentView === "logs" ? this.refreshLogs() : Promise.resolve(),
          this.lazyLoaded.tasks || this.currentView === "tasks" ? this.refreshTasks() : Promise.resolve(),
          this.lazyLoaded.recentUpdates || this.currentView === "recent-updates" ? this.refreshRecentUpdates() : Promise.resolve(),
          this.lazyLoaded.trash || this.currentView === "trash" ? this.refreshTrash() : Promise.resolve(),
          this.lazyLoaded.sites || this.isSitePanelActive() ? this.refreshSites() : Promise.resolve(),
          this.currentView === "gallery" ? this.refreshGallery(true) : Promise.resolve(),
          this.currentView === "settings" ? this.loadSettings() : Promise.resolve(),
        ]);
      } else if (previousRunning && !this.pullStatus.running) {
        await Promise.all([
          this.refreshMeta(),
          this.loadSubscriptions(),
          this.lazyLoaded.tasks || this.currentView === "tasks" ? this.refreshTasks() : Promise.resolve(),
          this.lazyLoaded.recentUpdates || this.currentView === "recent-updates" ? this.refreshRecentUpdates() : Promise.resolve(),
          this.lazyLoaded.duplicates || this.currentView === "duplicates" ? this.refreshDuplicates() : Promise.resolve(),
          this.lazyLoaded.sites || this.isSitePanelActive() ? this.refreshSites() : Promise.resolve(),
          this.currentView === "gallery" ? this.refreshGallery(true) : Promise.resolve(),
          this.currentView === "settings" ? this.loadSettings() : Promise.resolve(),
        ]);
      }
      if ((currentSiteTaskId && currentSiteTaskId !== previousSiteTaskId && !this.siteStatus.running) || (previousSiteRunning && !this.siteStatus.running)) {
        await Promise.all([
          this.refreshMeta(),
          this.loadSubscriptions(),
          this.lazyLoaded.tasks || this.currentView === "tasks" ? this.refreshTasks() : Promise.resolve(),
          this.lazyLoaded.recentUpdates || this.currentView === "recent-updates" ? this.refreshRecentUpdates() : Promise.resolve(),
          this.lazyLoaded.duplicates || this.currentView === "duplicates" ? this.refreshDuplicates() : Promise.resolve(),
          this.lazyLoaded.sites || this.isSitePanelActive() ? this.refreshSites() : Promise.resolve(),
          this.currentView === "gallery" ? this.refreshGallery(true) : Promise.resolve(),
        ]);
      }
    },

    openTasks() {
      this.closeGalleryLayers();
      this.currentView = "tasks";
      this.closeSidebarDrawer();
      this.scrollViewTop();
      this.refreshTasks();
      this.refreshSidebarCounts(["tasks"]).catch(() => {});
    },

    openTrash() {
      this.closeGalleryLayers();
      this.currentView = "trash";
      this.closeSidebarDrawer();
      this.scrollViewTop();
      this.refreshTrash();
      this.refreshSidebarCounts(["trash"]).catch(() => {});
    },

    askTrashCurrentFolder() {
      this.pendingTrashFolder = this.detail.folder?.folder_name || null;
    },

    askTrashFolder(folderName) {
      this.pendingTrashFolder = folderName || null;
    },

    cancelTrashCurrentFolder() {
      this.pendingTrashFolder = null;
    },

    galleryCardRects() {
      if (typeof document === "undefined" || !document.querySelectorAll) {
        return new Map();
      }
      const rects = new Map();
      document.querySelectorAll(".gallery-item-card[data-gallery-key]").forEach((element) => {
        const key = element.getAttribute("data-gallery-key");
        if (!key || element.classList.contains("deleted-placeholder-card")) {
          return;
        }
        rects.set(key, element.getBoundingClientRect());
      });
      return rects;
    },

    galleryCardRectFor(item) {
      const key = this.galleryItemKey(item);
      if (!key) {
        return null;
      }
      const element = document.querySelector(`[data-gallery-key="${this.cssEscape(key)}"]`);
      return element ? element.getBoundingClientRect() : null;
    },

    animateGalleryReflow(update) {
      const before = this.galleryCardRects();
      update();
      this.$nextTick(() => {
        const moving = [];
        document.querySelectorAll(".gallery-item-card[data-gallery-key]").forEach((element) => {
          const key = element.getAttribute("data-gallery-key");
          const previous = before.get(key);
          if (!previous || element.classList.contains("deleted-placeholder-card")) {
            return;
          }
          const current = element.getBoundingClientRect();
          const deltaX = previous.left - current.left;
          const deltaY = previous.top - current.top;
          if (Math.abs(deltaX) < 1 && Math.abs(deltaY) < 1) {
            return;
          }
          element.style.transition = "none";
          element.style.transform = `translate3d(${deltaX}px, ${deltaY}px, 0)`;
          moving.push(element);
        });
        window.requestAnimationFrame(() => {
          moving.forEach((element) => {
            element.style.transition = "transform 420ms cubic-bezier(0.22, 1, 0.36, 1)";
            element.style.transform = "";
          });
          if (this.galleryReflowTimer) {
            window.clearTimeout(this.galleryReflowTimer);
          }
          this.galleryReflowTimer = window.setTimeout(() => {
            moving.forEach((element) => {
              element.style.transition = "";
            });
            this.galleryReflowTimer = null;
          }, 450);
        });
      });
    },

    detailPairKey(pair) {
      return `${this.detail.folder?.folder_name || "detail"}::${pair?.pair_index || 0}`;
    },

    detailCardRects() {
      if (typeof document === "undefined" || !document.querySelectorAll) {
        return new Map();
      }
      const rects = new Map();
      document.querySelectorAll(".detail-card[data-detail-key]").forEach((element) => {
        const key = element.getAttribute("data-detail-key");
        if (key) {
          rects.set(key, element.getBoundingClientRect());
        }
      });
      return rects;
    },

    animateDetailReflow(update) {
      const before = this.detailCardRects();
      update();
      this.$nextTick(() => {
        const moving = [];
        document.querySelectorAll(".detail-card[data-detail-key]").forEach((element) => {
          const key = element.getAttribute("data-detail-key");
          const previous = before.get(key);
          if (!previous) {
            return;
          }
          const current = element.getBoundingClientRect();
          const deltaX = previous.left - current.left;
          const deltaY = previous.top - current.top;
          if (Math.abs(deltaX) < 1 && Math.abs(deltaY) < 1 && Math.abs(previous.height - current.height) < 1) {
            return;
          }
          element.style.transition = "none";
          element.style.transform = `translate3d(${deltaX}px, ${deltaY}px, 0)`;
          moving.push(element);
        });
        window.requestAnimationFrame(() => {
          moving.forEach((element) => {
            element.style.transition = "transform 360ms cubic-bezier(0.22, 1, 0.36, 1)";
            element.style.transform = "";
          });
          window.setTimeout(() => {
            moving.forEach((element) => {
              element.style.transition = "";
            });
          }, 390);
        });
      });
    },

    galleryDeletedPlaceholder(folderName, sourceItem, sourceRect = null, placeholderKey = folderName) {
      const ratio = sourceItem?.display_ratio || "4 / 5";
      return {
        __deleted_placeholder: true,
        __placeholder_id: `${folderName}:${Date.now()}`,
        __placeholder_key: placeholderKey,
        item_key: `deleted:${placeholderKey}:${Date.now()}`,
        folder_name: folderName,
        pair_index: sourceItem?.pair_index || null,
        title: sourceItem?.title || folderName,
        display_ratio: ratio,
        __placeholder_height: sourceRect?.height || null,
        __placeholder_width: sourceRect?.width || null,
        preview_tiles: [],
        asset_count: 0,
        image_count: 0,
        livephoto_count: 0,
      };
    },

    removeDeletedPlaceholder(placeholderKey) {
      this.animateGalleryReflow(() => {
        this.gallery = {
          ...this.gallery,
          items: (this.gallery.items || []).filter(
            (item) => !(item.__deleted_placeholder && (item.__placeholder_key || item.folder_name) === placeholderKey),
          ),
        };
      });
      if (this.deletedGalleryTimers[placeholderKey]) {
        window.clearTimeout(this.deletedGalleryTimers[placeholderKey]);
        delete this.deletedGalleryTimers[placeholderKey];
      }
    },

    async trashCurrentFolder() {
      const folderName = this.detail.folder?.folder_name;
      if (!folderName || this.pendingTrashFolder !== folderName) {
        return;
      }
      const previousGalleryItems = [...(this.gallery.items || [])];
      const previousGalleryTotal = this.gallery.total || 0;
      const previousRecentUpdates = this.cloneRecentUpdates();
      const previousRecentUpdateDisclosure = this.cloneRecentUpdateDisclosure();
      const previousDetail = {
        open: this.detail.open,
        pairs: [...(this.detail.pairs || [])],
        folder: this.detail.folder ? { ...this.detail.folder } : null,
        videos: [...(this.detail.videos || [])],
      };
      const previousCache = this.detailCache[folderName] ? this.cloneDetailPayload(this.detailCache[folderName]) : null;
      this.pendingTrashFolder = null;
      this.closeViewer();
      this.closeDetail();
      this.removeFolderFromGallery(folderName, { placeholder: true });
      this.invalidateDetailCache(folderName);
      this.clearLocalGalleryCaches();
      this.notify("info", "已提交删除", "");
      try {
        const result = await this.api(`/api/gallery/folders/${encodeURIComponent(folderName)}/trash`, {
          method: "POST",
          body: JSON.stringify({ reason: "不喜欢" }),
        });
        this.schedulePostMutationRefresh(result, ["all", "favorites", "livephoto", "trash", "tasks"], { trash: true, tasks: true });
      } catch (error) {
        if (this.deletedGalleryTimers[folderName]) {
          window.clearTimeout(this.deletedGalleryTimers[folderName]);
          delete this.deletedGalleryTimers[folderName];
        }
        this.gallery = {
          ...this.gallery,
          items: previousGalleryItems,
          total: previousGalleryTotal,
        };
        this.recentUpdates = previousRecentUpdates;
        this.restoreRecentUpdateDisclosure(previousRecentUpdateDisclosure);
        if (!this.viewer.open && !this.detail.open && previousDetail.folder?.folder_name === folderName) {
          this.detail = previousDetail;
        }
        if (previousCache) {
          this.cacheDetailPayload(folderName, previousCache);
        }
        this.syncBodyLock();
        this.notify("error", "移入垃圾桶失败", error.message || "后端删除没有成功，内容已恢复。");
      }
    },

    async trashFolderCard(folderName) {
      if (!folderName || this.pendingTrashFolder !== folderName) {
        return;
      }
      const folder = (this.gallery.items || []).find((item) => item.folder_name === folderName);
      const previousGalleryItems = [...(this.gallery.items || [])];
      const previousGalleryTotal = this.gallery.total || 0;
      const previousRecentUpdates = this.cloneRecentUpdates();
      const previousRecentUpdateDisclosure = this.cloneRecentUpdateDisclosure();
      const previousCache = this.detailCache[folderName] ? this.cloneDetailPayload(this.detailCache[folderName]) : null;
      this.pendingTrashFolder = null;
      if (this.videoNote.folder?.folder_name === folderName) {
        this.closeVideoNote();
      }
      if (this.detail.folder?.folder_name === folderName) {
        this.closeDetail();
      }
      this.removeFolderFromGallery(folderName, { placeholder: true });
      this.invalidateDetailCache(folderName);
      this.clearLocalGalleryCaches();
      this.notify("info", "已提交删除", "");
      try {
        const result = await this.api(`/api/gallery/folders/${encodeURIComponent(folderName)}/trash`, {
          method: "POST",
          body: JSON.stringify({ reason: "不喜欢" }),
        });
        this.schedulePostMutationRefresh(result, ["all", "favorites", "livephoto", "trash", "tasks"], { trash: true, tasks: true });
      } catch (error) {
        this.gallery = {
          ...this.gallery,
          items: previousGalleryItems,
          total: previousGalleryTotal,
        };
        this.recentUpdates = previousRecentUpdates;
        this.restoreRecentUpdateDisclosure(previousRecentUpdateDisclosure);
        if (previousCache) {
          this.cacheDetailPayload(folderName, previousCache);
        }
        if (folder && !this.gallery.items.find((item) => item.folder_name === folderName)) {
          this.gallery = { ...this.gallery, items: [folder, ...(this.gallery.items || [])] };
        }
        this.notify("error", "移入垃圾桶失败", error.message || "后端删除没有成功，内容已恢复。");
      }
    },

    removeFolderFromGallery(folderName, options = {}) {
      this.removeFolderFromRecentUpdates(folderName);
      const currentItems = this.gallery.items || [];
      const firstIndex = currentItems.findIndex((item) => item.folder_name === folderName && !item.__deleted_placeholder);
      const sourceItem = firstIndex >= 0 ? currentItems[firstIndex] : null;
      const sourceRect = sourceItem ? this.galleryCardRectFor(sourceItem) : null;
      const nextItems = currentItems.filter((item) => item.folder_name !== folderName || item.__deleted_placeholder);
      const removedCount = currentItems.length - nextItems.length;
      if (removedCount <= 0) {
        return;
      }
      if (options.placeholder && firstIndex >= 0) {
        nextItems.splice(firstIndex, 0, this.galleryDeletedPlaceholder(folderName, sourceItem, sourceRect));
      }
      this.gallery = {
        ...this.gallery,
        items: nextItems,
        total: Math.max(0, (this.gallery.total || 0) - removedCount),
      };
      if (options.placeholder) {
        if (this.deletedGalleryTimers[folderName]) {
          window.clearTimeout(this.deletedGalleryTimers[folderName]);
        }
        this.deletedGalleryTimers[folderName] = window.setTimeout(() => this.removeDeletedPlaceholder(folderName), 1500);
      }
    },

    removePairFromGallery(folderName, pairIndex, options = {}) {
      const currentItems = this.gallery.items || [];
      const firstIndex = currentItems.findIndex(
        (item) => item.folder_name === folderName && Number(item.pair_index) === Number(pairIndex) && !item.__deleted_placeholder,
      );
      const sourceItem = firstIndex >= 0 ? currentItems[firstIndex] : null;
      const sourceRect = sourceItem ? this.galleryCardRectFor(sourceItem) : null;
      const nextItems = currentItems.filter(
        (item) => !(item.folder_name === folderName && Number(item.pair_index) === Number(pairIndex)),
      );
      const removedCount = currentItems.length - nextItems.length;
      if (removedCount <= 0) {
        return;
      }
      const placeholderKey = this.pairPlaceholderKey(folderName, pairIndex);
      if (options.placeholder && firstIndex >= 0) {
        nextItems.splice(firstIndex, 0, this.galleryDeletedPlaceholder(folderName, sourceItem, sourceRect, placeholderKey));
      }
      this.gallery = {
        ...this.gallery,
        items: nextItems,
        total: Math.max(0, (this.gallery.total || 0) - removedCount),
      };
      if (options.placeholder) {
        if (this.deletedGalleryTimers[placeholderKey]) {
          window.clearTimeout(this.deletedGalleryTimers[placeholderKey]);
        }
        this.deletedGalleryTimers[placeholderKey] = window.setTimeout(() => this.removeDeletedPlaceholder(placeholderKey), 1500);
      }
    },

    replaceSubscriptionLocally(uid, mapper) {
      const normalized = String(uid);
      this.subscriptions = (this.subscriptions || []).map((item) =>
        String(item.uid) === normalized ? mapper({ ...item }) : item,
      );
    },

    askRestoreTrash(itemId) {
      this.pendingRestoreTrashId = itemId;
    },

    cancelRestoreTrash() {
      this.pendingRestoreTrashId = null;
    },

    async restoreTrash(itemId, repullNow) {
      const previousTrash = [...(this.trashItems || [])];
      this.pendingRestoreTrashId = null;
      this.trashItems = (this.trashItems || []).filter((item) => Number(item.id) !== Number(itemId));
      this.notify("info", "正在恢复内容", repullNow ? "页面已先更新，后台会继续尝试立即拉取。" : "页面已先更新，后台正在恢复。");
      try {
        const result = await this.api(`/api/trash/${itemId}/restore`, {
          method: "POST",
          body: JSON.stringify({ repull_now: !!repullNow }),
        });
        await Promise.all([this.refreshTrash(), this.refreshMeta(), this.refreshGallery(true), this.refreshTasks()]);
        this.notify("success", "恢复完成", result.message);
      } catch (error) {
        this.trashItems = previousTrash;
        this.notify("error", "恢复失败", error.message || "垃圾桶列表已恢复。");
      }
    },

    async startQrLogin() {
      this.qr = await this.api("/api/auth/qr/start", { method: "POST" });
      this.notify("info", "二维码已生成", "请使用哔哩哔哩客户端扫码。");
    },

    async pollQrStatus() {
      const payload = await this.api("/api/auth/qr/status");
      this.qr = { ...this.qr, ...payload };
      if (payload.status === "scanned") {
        return;
      }
      if (payload.status === "done") {
        await this.loadSettings();
        this.qr = { status: "done", message: payload.message };
        this.notify("success", "登录成功", payload.user?.message || "账号权限已经导入到当前页面。");
        return;
      }
      if (payload.status === "expired") {
        this.notify("error", "二维码已过期", payload.message || "请重新生成二维码。");
      }
    },

    async checkAuth() {
      this.settings.auth = await this.api("/api/auth/check");
    },

    async logout() {
      await this.api("/api/auth/logout", { method: "POST" });
      this.qr = {};
      await this.loadSettings();
      this.notify("success", "已退出登录", "本地保存的 Cookie 已清除。");
    },

    async refreshSites() {
      await Promise.all([
        this.loadSiteSources(),
        this.loadSiteRules(),
        this.loadSiteLogs(),
      ]);
      this.lazyLoaded.sites = true;
    },

    async loadSiteSources() {
      const payload = await this.api("/api/site-sources");
      const nextSources = (payload.items || []).filter((source) => source.source_type !== "xhs_likes");
      const previousDrafts = { ...(this.siteSourceDrafts || {}) };
      const previousExpanded = { ...(this.siteSourceExpanded || {}) };
      this.siteSourceDrafts = Object.fromEntries(
        nextSources.map((source) => [
          String(source.id),
          previousDrafts[String(source.id)] || this.siteSourceDraftFromSource(source),
        ]),
      );
      this.siteSourceExpanded = Object.fromEntries(
        nextSources.map((source) => [String(source.id), !!previousExpanded[String(source.id)]]),
      );
      this.siteSources = nextSources;
      this.sidebarCounts = { ...this.sidebarCounts, sites: nextSources.length };
    },

    async syncAllXhs() {
      if (this.xhsBusy) return;
      this.xhsBusy = true;
      try {
        const result = await this.api('/api/xhs/sync', { method: 'POST', body: '{}' });
        await Promise.all([this.refreshTasks(), this.refreshStatus(), this.loadXhsSources()]);
        this.notify('success', '小红书同步已提交', result.message);
      } catch (error) { this.notify('error', '无法开始同步', error.message); }
      finally { this.xhsBusy = false; }
    },

    async loadXhsSources() {
      const loginRevision = this.xhsLoginRevision;
      const payload = await this.api("/api/xhs/status");
      if (loginRevision !== this.xhsLoginRevision) return;
      this.xhsWorker = payload.worker || { state: 'NOT_CONFIGURED', owner_key: null };
      if (this.xhsWorker.state === 'LOGIN_IN_PROGRESS' && !this.xhsLoginSession && !this.xhsLoginStarting) {
        this.xhsLoginSession = { state: 'LOGIN_IN_PROGRESS' };
        this.startXhsQrPolling();
      } else if (this.xhsWorker.state !== 'LOGIN_IN_PROGRESS' && !this.xhsLoginStarting) {
        this.xhsLoginSession = null;
        this.clearXhsLoginUi();
      }
      this.xhsAutomaticAvailable = !!payload.automatic_sync_available;
      if (!this.xhsNewSource.owner_key && this.xhsWorker.owner_key) this.xhsNewSource.owner_key = this.xhsWorker.owner_key;
      this.xhsSources = payload.items || [];
      this.xhsChecks = Object.fromEntries(this.xhsSources.map(source => [String(source.id), this.xhsChecks[String(source.id)] ||
        { owner_confirmed: false, paging_confirmed: false, rules_reviewed: false, ordering_stable: false }]));
      this.xhsDrafts = Object.fromEntries(this.xhsSources.map((source) => {
        const draft = this.xhsDrafts[String(source.id)];
        return [String(source.id), draft?.config_version === source.config_version ? draft : this.xhsDraft(source)];
      }));
    },

    xhsDraft(source) {
      return { anchor_count: source.anchor_count, initial_fetch_limit: source.initial_fetch_limit,
        enabled: !!source.enabled, config_version: source.config_version,
        refresh_enabled: !!source.refresh_enabled, refresh_interval_minutes: source.refresh_interval_minutes,
        ordering_stable: !!source.ordering_stable, full_scan_interval_days: source.full_scan_interval_days ?? 7 };
    },

    xhsLimitValue(value) {
      if (value === null || value === "") return null;
      const number = Number(value);
      if (!Number.isInteger(number) || number < 1 || number > 10000) throw new Error("首次拉取条数须为 1–10000，留空为不限");
      return number;
    },

    xhsAuthText(state) {
      return { NOT_CONFIGURED: '当前运行环境缺少内置浏览器', LOGIN_REQUIRED: '等待扫码登录', MANUAL_ONLY: '清单导入模式',
        LOGIN_IN_PROGRESS: '等待扫码确认', READY: '已登录', RUNNING: '正在读取', NEEDS_REAUTH: '登录已失效',
        NEEDS_REBIND: '账号不一致', NEEDS_MANUAL_VERIFICATION: '需要人工验证', RATE_LIMITED: '请求受限，已暂停',
        STRUCTURE_CHANGED: '页面结构变化，已暂停', CURSOR_ERROR: '分页异常，已暂停',
        UNAUTHORIZED: '浏览器服务凭证不匹配', BROWSER_UNAVAILABLE: '浏览器暂不可用',
        BROWSER_SANDBOX_BLOCKED: '容器配置阻止了浏览器沙箱',
        BROWSER_DISPLAY_UNAVAILABLE: '浏览器显示服务未就绪', BROWSER_NOT_INSTALLED: '镜像缺少浏览器',
        BROWSER_MISSING_DEPENDENCIES: '浏览器运行库缺失', BROWSER_UNSUPPORTED_CPU: '处理器与浏览器不兼容',
        BROWSER_OUT_OF_MEMORY: '浏览器启动内存不足',
        CANCELLED: '已取消', WORKER_ERROR: '浏览器服务暂不可用' }[state] || state;
    },

    clearXhsLoginUi() {
      this.stopXhsQrPolling();
      this.xhsQr = null;
      this.xhsBrowserVisible = false;
    },

    xhsQrText(phase) {
      return { preparing: '正在准备登录二维码…', await_scan: '请使用小红书 App 扫码并在手机上确认登录。',
        scanned: '已扫码，请在手机上确认登录。', refreshing: '二维码已刷新，请重新扫码。',
        finalizing: '已检测到登录，正在确认账号信息…', logged_in: '登录成功，正在完成…',
        captcha: '请在下方验证画面中完成安全验证，然后继续扫码登录。',
        blocked: '小红书显示安全限制，请查看验证画面中的提示。',
        missing: '未能取得二维码，可打开验证画面检查当前页面。',
        error: '二维码获取失败，请重新启动登录。' }[phase] || '正在准备登录二维码…';
    },

    startXhsQrPolling() {
      this.stopXhsQrPolling();
      this.xhsQrErrors = 0;
      this.xhsQrAutoFinishTried = false;
      this.xhsQrFinishRetryAt = 0;
      this.pollXhsLoginQr();
      this.xhsQrTimer = window.setInterval(() => { this.pollXhsLoginQr(); }, 2500);
    },

    stopXhsQrPolling() {
      if (this.xhsQrTimer !== null && this.xhsQrTimer !== undefined) window.clearInterval(this.xhsQrTimer);
      this.xhsQrTimer = null;
    },

    async pollXhsLoginQr() {
      if (this.xhsQrBusy || this.xhsQrFinishing || !this.xhsLoginSession) return;
      this.xhsQrBusy = true;
      const revision = this.xhsLoginRevision;
      try {
        const result = await this.api('/api/xhs/login/qr', { method: 'POST', body: '{}' });
        if (revision !== this.xhsLoginRevision) return;
        this.xhsQrErrors = 0;
        if (['captcha', 'missing', 'blocked'].includes(result.phase) && result.phase !== this.xhsQr?.phase) {
          this.xhsBrowserVisible = true;
        }
        this.xhsQr = { image_data_url: result.image_data_url || null,
          phase: result.phase || 'preparing', message: result.message || '' };
        if (result.phase === 'logged_in' && !this.xhsQrAutoFinishTried && !this.xhsBusy && Date.now() >= (this.xhsQrFinishRetryAt || 0)) {
          this.xhsQrAutoFinishTried = true;
          this.xhsQrFinishing = true;
          try { await this.xhsLogin('finish'); } finally { this.xhsQrFinishing = false; }
        }
      } catch (error) {
        if (revision !== this.xhsLoginRevision) return;
        this.xhsQrErrors += 1;
        if (this.xhsQrErrors >= 8) {
          this.stopXhsQrPolling();
          this.xhsQr = { image_data_url: null, phase: 'error',
            message: error.message || '二维码获取失败，请重新启动登录。' };
        }
      } finally {
        this.xhsQrBusy = false;
      }
    },

    async xhsLogin(action) {
      if (this.xhsBusy) return;
      if (action === 'revoke' && !window.confirm('确认注销小红书浏览器登录态？对应订阅会停用，已归档媒体保留。')) return;
      this.xhsLoginRevision += 1;
      if (action === 'start') {
        // 二维码直接在页面展示，登录检测失败时保留会话供重试。
        this.xhsLoginStarting = true;
        this.stopXhsQrPolling();
        this.xhsQr = null;
        this.xhsBrowserVisible = false;
      }
      this.xhsBusy = true;
      try {
        const result = await this.api(`/api/xhs/login/${action}`, { method: 'POST',
          body: JSON.stringify(action === 'revoke' ? { confirm_revoke: true } : {}) });
        if (action === 'start') {
          window.clearTimeout(this.xhsLoginTimer);
          this.xhsLoginSession = result;
          this.startXhsQrPolling();
          this.xhsLoginTimer = window.setTimeout(() => {
            this.xhsLoginSession = null; this.clearXhsLoginUi();
          }, (result.expires_in || 600) * 1000);
        } else if (action !== 'check' || result.state !== 'LOGIN_IN_PROGRESS') {
          window.clearTimeout(this.xhsLoginTimer);
          this.xhsLoginSession = null;
          this.clearXhsLoginUi();
        }
        await Promise.all([this.loadXhsSources(), this.loadSubscriptions()]);
        this.notify('success', '小红书账号状态已更新', this.xhsAuthText(result.state));
      } catch (error) {
        if (action === 'finish' && this.xhsLoginSession) {
          this.xhsQrAutoFinishTried = false;
          this.xhsQrFinishRetryAt = Date.now() + 10000;
        }
        if (action === 'start') {
          window.clearTimeout(this.xhsLoginTimer);
          this.xhsLoginSession = null;
          this.clearXhsLoginUi();
        }
        this.notify('error', '账号操作未完成', error.message);
        await this.loadXhsSources().catch(() => {});
      }
      finally { this.xhsBusy = false; this.xhsLoginStarting = false; }
    },

    async xhsRelogin() {
      // Also expose the status and fallback controls in the subscription card.
      this.xhsCreateExpanded = true;
      await this.xhsLogin('start');
    },

    async verifyXhsSource(source) {
      this.xhsBusy = true;
      try {
        await this.api(`/api/xhs/sources/${source.id}/verify`, { method: 'POST', body: JSON.stringify(this.xhsChecks[String(source.id)]) });
        await this.loadXhsSources();
        this.notify('success', '本人点赞源已验证', '可立即同步，自动刷新使用此来源独立设置的周期。');
      } catch (error) { this.notify('error', '验证未完成', error.message); }
      finally { this.xhsBusy = false; }
    },

    async syncXhsSource(source, options = {}) {
      if (this.xhsBusy) return;
      this.xhsBusy = true;
      try {
        const result = await this.api(`/api/xhs/sources/${source.id}/sync`, { method: 'POST', body: JSON.stringify(options) });
        await Promise.all([this.refreshTasks(), this.refreshStatus()]);
        this.notify('success', '小红书同步已提交', result.message);
      } catch (error) { this.notify('error', '无法开始同步', error.message); }
      finally { this.xhsBusy = false; }
    },

    async releaseXhsNote(source, note) {
      await this.api(`/api/xhs/sources/${source.id}/notes/${encodeURIComponent(note.note_id)}/release`, { method: 'POST', body: '{}' });
      await this.loadXhsNotes(source);
      this.notify('success', '笔记已放行', '点击重试媒体完成下载。');
    },

    async checkXhsFiles(source) {
      if (this.xhsBusy) return;
      this.xhsBusy = true;
      try {
        const result = await this.api(`/api/xhs/sources/${source.id}/check-files`, { method: 'POST', body: '{}' });
        await Promise.all([this.refreshTasks(), this.refreshStatus()]);
        this.notify('success', '本地归档检查已提交', result.message);
      } catch (error) { this.notify('error', '无法检查归档', error.message); }
      finally { this.xhsBusy = false; }
    },

    xhsSourceDirty(source) {
      const draft = this.xhsDrafts[String(source.id)];
      if (!draft) return false;
      return Number(draft.anchor_count) !== source.anchor_count || draft.enabled !== !!source.enabled
        || draft.ordering_stable !== !!source.ordering_stable || Number(draft.full_scan_interval_days) !== (source.full_scan_interval_days ?? 7)
        || Number(draft.refresh_interval_minutes) !== source.refresh_interval_minutes || draft.refresh_enabled !== !!source.refresh_enabled
        || (draft.initial_fetch_limit === "" || draft.initial_fetch_limit === null ? null : Number(draft.initial_fetch_limit)) !== source.initial_fetch_limit;
    },

    async createXhsSource() {
      if (this.xhsBusy) return;
      this.xhsBusy = true;
      try {
        const payload = { ...this.xhsNewSource, anchor_count: Number(this.xhsNewSource.anchor_count),
          refresh_interval_minutes: Number(this.xhsNewSource.refresh_interval_minutes),
          initial_fetch_limit: this.xhsLimitValue(this.xhsNewSource.initial_fetch_limit) };
        await this.api("/api/xhs/sources", { method: "POST", body: JSON.stringify(payload) });
        this.xhsNewSource.owner_key = "";
        this.xhsCreateExpanded = false;
        await Promise.all([this.loadXhsSources(), this.loadSubscriptions()]);
        this.notify("success", "小红书来源已创建", "可导入与该账号标识匹配的净化 JSON 清单。");
      } catch (error) {
        this.notify("error", "创建失败", error.message);
      } finally { this.xhsBusy = false; }
    },

    async saveXhsSource(source) {
      if (this.xhsBusy) return;
      this.xhsBusy = true;
      try {
        const draft = this.xhsDrafts[String(source.id)];
        const saved = await this.api(`/api/xhs/sources/${source.id}/anchor`, { method: "PATCH",
          body: JSON.stringify({ ...draft, anchor_count: Number(draft.anchor_count),
            refresh_interval_minutes: Number(draft.refresh_interval_minutes),
            full_scan_interval_days: Number(draft.full_scan_interval_days),
            initial_fetch_limit: this.xhsLimitValue(draft.initial_fetch_limit) }) });
        this.xhsDrafts[String(source.id)] = this.xhsDraft(saved);
        await this.loadXhsSources();
        this.notify("success", "小红书设置已保存");
      } catch (error) {
        this.notify("error", "保存失败", error.message);
      } finally { this.xhsBusy = false; }
    },

    async importXhsManifest(source, event) {
      const input = event.target;
      const file = input.files?.[0];
      if (!file || this.xhsBusy) { input.value = ""; return; }
      this.xhsBusy = true;
      try {
        if (file.size > 4 * 1024 * 1024) throw new Error("清单不能超过 4 MiB");
        const result = await this.api(`/api/xhs/manifest/import?source_id=${source.id}`, {
          method: "POST", body: await file.text(),
        });
        this.xhsNotes = {};
        await Promise.all([this.loadXhsSources(), this.loadSubscriptions(), this.refreshTasks(), this.refreshStatus()]);
        this.notify("success", result.replayed ? "清单已导入过" : "小红书清单已导入",
          `读取 ${result.seen_count} 条，新增 ${result.new_count} 条。${this.xhsStopText(result.stop_reason)}。媒体待解析。`);
      } catch (error) {
        this.notify("error", "导入失败", error.message);
      } finally { input.value = ""; this.xhsBusy = false; }
    },

    async loadXhsNotes(source) {
      const payload = await this.api(`/api/xhs/sources/${source.id}/notes`);
      this.xhsNotes = { ...this.xhsNotes, [source.id]: payload.items || [] };
    },

    openXhsNote(source, note) {
      if (note.folder_name) {
        const isVideo = note.note_type === 'video';
        this.openFolder(note.folder_name, {
          folder_name: note.folder_name,
          title: note.title || note.note_id,
          original_url: note.url || `https://www.xiaohongshu.com/explore/${note.note_id}`,
          video_count: isVideo ? 1 : 0,
          image_count: isVideo ? 0 : 1,
          livephoto_count: 0,
          has_livephoto: false,
          has_images: !isVideo,
        });
      } else {
        const url = note.url || `https://www.xiaohongshu.com/explore/${note.note_id}`;
        this.sourcePreview = {
          open: true,
          url,
          title: note.title || note.note_id,
          subtitle: url,
          reviewId: null,
        };
        this.syncBodyLock();
      }
    },

    async confirmXhsReset(source) {
      if (this.xhsBusy) return;
      const step = Number(this.xhsResetSteps[source.id] || 0);
      if (step < 3) {
        this.xhsResetSteps = { ...this.xhsResetSteps, [source.id]: step + 1 };
        return;
      }
      this.xhsBusy = true;
      try {
        const result = await this.api(`/api/xhs/sources/${source.id}/reset`, { method: 'POST',
          body: JSON.stringify({ confirmations: 3, config_version: source.config_version }) });
        this.closeGalleryLayers();
        this.detailCache = {};
        this.detailCacheOrder = [];
        this.clearLocalGalleryCaches();
        this.xhsNotes = {};
        await Promise.all([this.loadXhsSources(), this.loadSubscriptions(), this.refreshTasks(), this.refreshStatus(), this.refreshGallery(true)]);
        this.notify('success', '小红书抓取已重置', result.message);
      } catch (error) {
        this.notify('error', '重置未完成', error.message);
      } finally {
        this.xhsResetSteps = { ...this.xhsResetSteps, [source.id]: 0 };
        this.xhsBusy = false;
      }
    },

    xhsStopText(reason) {
      return { manual_import: "手工导入，未核对完整点赞列表", first_sync_limit: "已达首次拉取条数，部分完成",
        anchor_hit: "命中上次任一锚点，增量完成", end_of_feed: "完整读取到列表末尾",
        anchor_miss_limit: "未命中锚点，已达增量止损上限，锚点已按本轮前部刷新",
        anchor_title_hit: "未命中锚点ID，读到同作者同标题笔记（疑似删旧重发），提前结束并刷新锚点",
        page_limit: "已达页数上限", time_limit: "已达时长上限", cursor_error: "分页不完整",
        cancelled: "已取消", network_error: "网络中断", structure_changed: "页面结构变化",
        needs_reauth: "需要重新登录", rate_limited: "请求受限" }[reason] || reason || "尚未读取";
    },

    async loadSiteRules() {
      this.siteRules = await this.api("/api/site-rules");
      this.siteRules.mode = this.normalizeSiteRuleMode(this.siteRules.mode);
      const legacy = this.siteRules.keywords || [];
      const allowFallback = this.siteRules.mode === "whitelist" ? legacy : [];
      const blockFallback = this.siteRules.mode === "blacklist" ? legacy : [];
      const allowKeywords = this.uniqueLines([
        ...(this.siteRules.allow_keywords || []),
        ...(this.siteRules.title_allow || []),
        ...(this.siteRules.tag_allow || []),
        ...allowFallback,
      ]);
      const blockKeywords = this.uniqueLines([
        ...(this.siteRules.block_keywords || []),
        ...(this.siteRules.title_block || []),
        ...(this.siteRules.tag_block || []),
        ...blockFallback,
      ]);
      this.siteRuleText.allow_keywords = this.joinLines(allowKeywords);
      this.siteRuleText.block_keywords = this.joinLines(blockKeywords);
      this.siteRuleText.title_allow = "";
      this.siteRuleText.title_block = "";
      this.siteRuleText.tag_allow = "";
      this.siteRuleText.tag_block = "";
    },

    normalizeSiteRuleMode(mode) {
      return ["whitelist", "blacklist", "both"].includes(mode) ? mode : "blacklist";
    },

    siteRuleModeLabel() {
      if (this.siteRules.mode === "whitelist") return "仅白名单";
      if (this.siteRules.mode === "both") return "黑白同时";
      return "仅黑名单";
    },

    siteRuleKeywordCount(kind) {
      const key = kind === "allow" ? "allow_keywords" : "block_keywords";
      return this.splitLines(this.siteRuleText[key]).length;
    },

    async loadSiteLogs() {
      const payload = await this.api("/api/site-filter/logs");
      this.siteLogs = payload.items || [];
    },

    resetSiteSourceForm() {
      this.siteSourceForm = {
        name: "",
        slug: "",
        source_type: "html",
        entry_url: "",
        page_url_template: "",
        max_pages: 1,
        list_item_selector: "",
        detail_link_selector: "a",
        title_selector: "h1",
        date_selector: "time",
        tag_selector: ".tag",
        body_selector: "article, .entry-content, .post-content, .library-content, .gallery-content, .content, main",
        media_selector: "article img, article video, article source, .entry-content img, .post-content img, .library-content img, .gallery-content img, .content img, .content video, .content source, main img",
        skip_head_images: 0,
        skip_tail_images: 0,
        use_proxy: true,
        enabled: true,
        start_date: "",
        icon_url: "",
      };
      this.sitePreviewItems = [];
      this.siteSuggestion = null;
      this.siteSourceFormExpanded = false;
    },

    siteSourceDraftFromSource(source) {
      return {
        id: source.id,
        name: source.name || "",
        slug: source.slug || "",
        source_type: source.source_type || "html",
        entry_url: source.entry_url || "",
        page_url_template: source.page_url_template || "",
        max_pages: Number(source.max_pages || 1),
        list_item_selector: source.list_item_selector || "",
        detail_link_selector: source.detail_link_selector || "a",
        title_selector: source.title_selector || "h1",
        date_selector: source.date_selector || "time",
        tag_selector: source.tag_selector || ".tag",
        body_selector: source.body_selector || "article, .entry-content, .post-content, .library-content, .gallery-content, .content, main",
        media_selector: source.media_selector || "article img, article video, article source, .entry-content img, .post-content img, .library-content img, .gallery-content img, .content img, .content video, .content source, main img",
        skip_head_images: Number(source.skip_head_images || 0),
        skip_tail_images: Number(source.skip_tail_images || 0),
        use_proxy: source.use_proxy !== false && Number(source.use_proxy ?? 1) !== 0,
        enabled: Boolean(source.enabled),
        start_date: source.start_date || "",
        icon_url: source.icon_url || "",
      };
    },

    isSiteSourceExpanded(sourceId) {
      return !!this.siteSourceExpanded[String(sourceId)];
    },

    toggleSiteSourceExpanded(sourceId) {
      const key = String(sourceId);
      this.siteSourceExpanded = {
        ...this.siteSourceExpanded,
        [key]: !this.siteSourceExpanded[key],
      };
    },

    resetSiteSourceDraft(source) {
      this.siteSourceDrafts = {
        ...this.siteSourceDrafts,
        [String(source.id)]: this.siteSourceDraftFromSource(source),
      };
      this.sitePreviewItemsById = {
        ...this.sitePreviewItemsById,
        [String(source.id)]: [],
      };
    },

    siteSourceDraft(sourceId) {
      const key = String(sourceId);
      if (!this.siteSourceDrafts[key]) {
        const source = (this.siteSources || []).find((item) => String(item.id) === key);
        this.siteSourceDrafts = {
          ...this.siteSourceDrafts,
          [key]: source ? this.siteSourceDraftFromSource(source) : {},
        };
      }
      return this.siteSourceDrafts[key];
    },

    siteSourceDraftValue(sourceId, field) {
      const draft = this.siteSourceDraft(sourceId);
      if (field === "enabled" || field === "use_proxy") {
        return field === "enabled" ? !!draft.enabled : draft.use_proxy !== false;
      }
      return draft[field] ?? "";
    },

    updateSiteSourceDraft(sourceId, field, value) {
      const key = String(sourceId);
      const numericFields = new Set(["max_pages", "skip_head_images", "skip_tail_images"]);
      const draft = this.siteSourceDraft(sourceId);
      let nextValue = value;
      if (numericFields.has(field)) {
        const fallback = field === "max_pages" ? 1 : 0;
        nextValue = Math.max(fallback, Number(value) || fallback);
      }
      if (field === "enabled" || field === "use_proxy") {
        nextValue = !!value;
      }
      this.siteSourceDrafts = {
        ...this.siteSourceDrafts,
        [key]: {
          ...draft,
          [field]: nextValue,
        },
      };
    },

    siteSourceStatusText(source) {
      return source.enabled ? "启用" : "停用";
    },

    siteSourceCountText(source) {
      return `贴文 ${source.post_count || 0} · 媒体 ${source.asset_count || 0}`;
    },

    siteSourceProxyText(source) {
      return source.use_proxy === false || Number(source.use_proxy ?? 1) === 0 ? "不使用站点代理" : "应用站点代理";
    },

    siteSourceSavingLabel(sourceId) {
      return this.siteSourceSavingById[String(sourceId)] ? "保存中" : "保存设置";
    },

    siteSourceTestingLabel(sourceId) {
      return this.siteTestLoadingById[String(sourceId)] ? "解析中" : "测试解析";
    },

    async saveSiteSource(sourceId = null) {
      const key = sourceId == null ? null : String(sourceId);
      if (key && this.siteSourceSavingById[key]) return;
      if (!key && this.siteSourceSaving) return;
      const draft = key ? this.siteSourceDraft(sourceId) : this.siteSourceForm;
      if (!draft?.entry_url) {
        this.notify("error", "保存失败", "请输入入口 URL。");
        return;
      }
      const body = JSON.stringify(draft);
      const url = draft.id
        ? `/api/site-sources/${encodeURIComponent(draft.id)}`
        : "/api/site-sources";
      const method = draft.id ? "PUT" : "POST";
      if (key) {
        this.siteSourceSavingById = { ...this.siteSourceSavingById, [key]: true };
      } else {
        this.siteSourceSaving = true;
      }
      this.notify("info", "正在保存站点设置", draft.name || "新站点来源");
      try {
        const result = await this.api(url, { method, body });
        this.notify("success", "站点来源已保存", result.message || "来源配置已更新。");
        if (key && result.item) {
          this.siteSourceDrafts = {
            ...this.siteSourceDrafts,
            [key]: this.siteSourceDraftFromSource(result.item),
          };
        }
        if (!key) {
          this.resetSiteSourceForm();
          this.newSiteSourceExpanded = false;
        }
        await Promise.all([this.loadSiteSources(), this.refreshStatus()]);
      } finally {
        if (key) {
          this.siteSourceSavingById = { ...this.siteSourceSavingById, [key]: false };
        } else {
          this.siteSourceSaving = false;
        }
      }
    },

    async deleteSiteSource(source) {
      const confirmed = window.confirm(`删除站点来源：${source.name}`);
      if (!confirmed) return;
      const result = await this.api(`/api/site-sources/${encodeURIComponent(source.id)}`, { method: "DELETE" });
      this.notify("success", "站点来源已删除", result.message || "来源已删除。");
      await Promise.all([this.loadSiteSources(), this.refreshStatus()]);
    },

    async refreshSiteSourceIcon(source) {
      const sourceId = Number(source?.id);
      const subscriptionUid = `site:${sourceId || ""}`;
      if (!sourceId || this.siteIconRefreshingById[String(sourceId)]) {
        return;
      }
      const key = String(sourceId);
      this.siteIconRefreshingById = { ...this.siteIconRefreshingById, [key]: true };
      this.iconLoadFailures = { ...this.iconLoadFailures, [subscriptionUid]: false };
      this.notify("info", "正在重新获取站点图标", "");
      try {
        const result = await this.api(`/api/site-sources/${encodeURIComponent(sourceId)}/refresh-icon`, { method: "POST" });
        if (!result.item) {
          throw new Error("图标刷新接口没有返回站点数据");
        }
        const refreshVersion = Date.now();
        this.siteSources = (this.siteSources || []).map((item) =>
          Number(item.id) === sourceId ? { ...item, ...result.item } : item,
        );
        this.subscriptions = (this.subscriptions || []).map((item) =>
          String(item.uid) === subscriptionUid
            ? this.normalizeSubscriptionItem({
                ...item,
                icon_url: result.item.icon_url || "",
                icon_tiny_url: result.item.icon_tiny_url || "",
                updated_at: result.item.updated_at || item.updated_at,
              })
            : item,
        );
        this.iconRefreshVersions = { ...this.iconRefreshVersions, [subscriptionUid]: refreshVersion };
        this.iconLoadFailures = { ...this.iconLoadFailures, [subscriptionUid]: false };
        await Promise.all([this.loadSiteSources(), this.loadSubscriptions()]);
        this.iconRefreshVersions = { ...this.iconRefreshVersions, [subscriptionUid]: refreshVersion };
        this.iconLoadFailures = { ...this.iconLoadFailures, [subscriptionUid]: false };
        this.notify(
          result.item.icon_url ? "success" : "info",
          result.item.icon_url ? "站点图标已更新" : "未找到可用站点图标",
          "",
        );
      } catch (error) {
        this.notify("error", "站点图标刷新失败", error.message || "请稍后重试。");
      } finally {
        this.siteIconRefreshingById = { ...this.siteIconRefreshingById, [key]: false };
      }
    },

    askSiteValidate(sourceId) {
      const normalized = Number(sourceId);
      if (this.siteValidateConfirmId !== normalized) {
        const source = (this.siteSources || []).find((item) => Number(item.id) === normalized);
        this.siteValidateConfirmId = normalized;
        this.siteValidateConfirmStep = 1;
        this.siteValidatePageDraftById = {
          ...this.siteValidatePageDraftById,
          [String(normalized)]: Math.max(1, Number(source?.max_pages || 1)),
        };
        return;
      }
      this.siteValidateConfirmStep = 2;
    },

    cancelSiteValidate() {
      this.siteValidateConfirmId = null;
      this.siteValidateConfirmStep = 0;
    },

    siteValidatePageDraft(sourceId) {
      const key = String(sourceId);
      const draft = Number(this.siteValidatePageDraftById[key]);
      if (Number.isFinite(draft) && draft > 0) {
        return draft;
      }
      const source = (this.siteSources || []).find((item) => String(item.id) === key);
      return Math.max(1, Number(source?.max_pages || 1));
    },

    updateSiteValidatePageDraft(sourceId, value) {
      const key = String(sourceId);
      this.siteValidatePageDraftById = {
        ...this.siteValidatePageDraftById,
        [key]: Math.max(1, Number(value) || 1),
      };
    },

    siteValidateLabel(sourceId) {
      if (this.siteValidateConfirmId !== Number(sourceId)) {
        return "全量校验";
      }
      return "再次确认校验";
    },

    askSiteClearDelete(sourceId) {
      const normalized = Number(sourceId);
      if (this.siteClearDeleteConfirmId !== normalized) {
        this.siteClearDeleteConfirmId = normalized;
        this.siteClearDeleteConfirmStep = 1;
        return;
      }
      this.siteClearDeleteConfirmStep = Math.min(this.siteClearDeleteConfirmStep + 1, 3);
    },

    cancelSiteClearDelete() {
      this.siteClearDeleteConfirmId = null;
      this.siteClearDeleteConfirmStep = 0;
    },

    siteClearDeleteLabel(sourceId) {
      if (this.siteClearDeleteConfirmId !== Number(sourceId)) {
        return "清空并删除";
      }
      if (this.siteClearDeleteConfirmStep === 1) {
        return "再次确认";
      }
      return "最后确认";
    },

    async confirmSiteClearDelete(sourceId) {
      if (this.siteClearDeleteConfirmId !== Number(sourceId) || this.siteClearDeleteConfirmStep < 3) {
        return;
      }
      this.cancelSiteClearDelete();
      const result = await this.api(`/api/site-sources/${encodeURIComponent(sourceId)}/clear-delete`, { method: "POST" });
      this.notify("success", "站点已清空并删除", result.message || "站点内容已移除。");
      await Promise.all([
        this.refreshSites(),
        this.refreshMeta(),
        this.loadSubscriptions(),
        this.refreshGallery(true),
        this.refreshStatus(),
      ]);
    },

    async testSiteSource(sourceId = null) {
      const key = sourceId == null ? null : String(sourceId);
      if (key && this.siteTestLoadingById[key]) return;
      if (!key && this.siteTestLoading) return;
      const draft = key ? this.siteSourceDraft(sourceId) : this.siteSourceForm;
      if (key) {
        this.siteTestLoadingById = { ...this.siteTestLoadingById, [key]: true };
        this.sitePreviewItemsById = { ...this.sitePreviewItemsById, [key]: [] };
      } else {
        this.siteTestLoading = true;
        this.sitePreviewItems = [];
      }
      try {
        const payload = await this.api("/api/site-sources/test", {
          method: "POST",
          body: JSON.stringify(draft),
        });
        if (key) {
          this.sitePreviewItemsById = { ...this.sitePreviewItemsById, [key]: payload.items || [] };
        } else {
          this.sitePreviewItems = payload.items || [];
        }
        this.notify("success", "解析完成", `解析到 ${(payload.items || []).length} 条预览。`);
      } finally {
        if (key) {
          this.siteTestLoadingById = { ...this.siteTestLoadingById, [key]: false };
        } else {
          this.siteTestLoading = false;
        }
      }
    },

    async suggestSiteSource() {
      if (this.siteSuggestLoading) return;
      const entryUrl = String(this.siteSourceForm.entry_url || "").trim();
      if (!entryUrl) {
        this.notify("error", "自动填写失败", "请输入入口 URL。");
        return;
      }
      this.siteSuggestLoading = true;
      this.siteSuggestion = null;
      try {
        const payload = await this.api("/api/site-sources/suggest", {
          method: "POST",
          body: JSON.stringify({ entry_url: entryUrl }),
        });
        const suggestion = payload.suggestion || {};
        this.siteSourceForm = this.applySiteSourceSuggestion(this.siteSourceForm, suggestion);
        this.sitePreviewItems = suggestion.preview || [];
        this.siteSuggestion = suggestion;
        this.siteSourceFormExpanded = true;
        this.notify("success", "自动填写完成", suggestion.message || "已生成站点解析参数。");
      } finally {
        this.siteSuggestLoading = false;
      }
    },

    applySiteSourceSuggestion(form, suggestion) {
      const next = { ...form };
      const fields = [
        "source_type",
        "entry_url",
        "page_url_template",
        "max_pages",
        "list_item_selector",
        "detail_link_selector",
        "title_selector",
        "date_selector",
        "tag_selector",
        "body_selector",
        "media_selector",
        "skip_head_images",
        "skip_tail_images",
        "use_proxy",
        "icon_url",
        "enabled",
        "start_date",
      ];
      for (const field of fields) {
        if (suggestion[field] !== undefined && suggestion[field] !== null) {
          next[field] = suggestion[field];
        }
      }
      for (const field of ["name", "slug"]) {
        if (!String(next[field] || "").trim() && suggestion[field]) {
          next[field] = suggestion[field];
        }
      }
      return next;
    },

    async syncSiteSource(source) {
      this.setImmediateSiteTaskFeedback(`正在提交 ${source.name || "站点"} 同步任务...`, { running: true });
      const result = await this.api(`/api/site-sources/${encodeURIComponent(source.id)}/sync`, { method: "POST" });
      this.notify("info", "站点同步已提交", result.message || "已开始同步。");
      await Promise.all([this.refreshStatus(), this.refreshTasks()]);
    },

    async validateSiteSource(source) {
      if (this.siteValidateConfirmId !== Number(source.id) || this.siteValidateConfirmStep < 2) {
        return;
      }
      this.cancelSiteValidate();
      this.setImmediateSiteTaskFeedback(`正在提交 ${source.name || "站点"} 全量校验任务...`, { running: true });
      const result = await this.api(`/api/site-sources/${encodeURIComponent(source.id)}/validate`, {
        method: "POST",
        body: JSON.stringify({ max_pages: this.siteValidatePageDraft(source.id) }),
      });
      this.notify("info", "站点全量校验已提交", result.message || "已开始全量校验。");
      await Promise.all([this.refreshStatus(), this.refreshTasks()]);
    },

    async pullSiteSubscription(item) {
      this.setImmediateSiteTaskFeedback(`正在提交 ${item.uname || "站点"} 拉取任务...`, { running: true });
      const result = await this.api(`/api/subscriptions/${encodeURIComponent(item.uid)}/pull`, { method: "POST" });
      this.notify("info", "站点拉取已提交", result.message || "已开始拉取当前站点。");
      await Promise.all([this.refreshStatus(), this.refreshTasks()]);
    },

    async syncAllSites() {
      this.setImmediateSiteTaskFeedback("正在提交全部站点同步任务...", { running: true });
      const result = await this.api("/api/site-sync", { method: "POST" });
      this.notify("info", "站点同步已提交", result.message || "已开始同步。");
      await Promise.all([this.refreshStatus(), this.refreshTasks()]);
    },

    async exportSiteSources() {
      const payload = await this.api("/api/site-sources/export");
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "site-sources.json";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      this.notify("success", "来源配置已导出", "站点来源 JSON 已生成。");
    },

    async importSiteSources(event) {
      const file = event.target.files && event.target.files[0];
      event.target.value = "";
      if (!file) return;
      const payload = JSON.parse(await file.text());
      const result = await this.api("/api/site-sources/import", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      await Promise.all([this.loadSiteSources(), this.refreshStatus()]);
      this.notify("success", "导入完成", `新增 ${result.created || 0}，更新 ${result.updated || 0}。`);
    },

    async saveSiteRules() {
      const allowKeywords = this.splitLines(this.siteRuleText.allow_keywords);
      const blockKeywords = this.splitLines(this.siteRuleText.block_keywords);
      const payload = {
        mode: this.normalizeSiteRuleMode(this.siteRules.mode),
        keywords: [],
        allow_keywords: allowKeywords,
        block_keywords: blockKeywords,
        title_allow: [],
        title_block: [],
        tag_allow: [],
        tag_block: [],
        use_regex: Boolean(this.siteRules.use_regex),
      };
      this.siteRules = await this.api("/api/site-rules", { method: "PUT", body: JSON.stringify(payload) });
      this.siteRules.mode = this.normalizeSiteRuleMode(this.siteRules.mode);
      this.notify("success", "站点规则已保存", "后续同步会使用新的过滤规则。");
    },

    async clearSiteLogs() {
      if (!this.siteLogsClearConfirm) {
        this.siteLogsClearConfirm = true;
        return;
      }
      this.siteLogsClearConfirm = false;
      const result = await this.api("/api/site-filter/logs/clear", { method: "POST" });
      this.siteLogs = [];
      await this.refreshStatus();
      this.notify("success", "站点过滤日志已清空", result.message || "历史记录已清理。");
    },

    cancelClearSiteLogs() {
      this.siteLogsClearConfirm = false;
    },

    joinLines(items) {
      return (items || []).join("\n");
    },

    uniqueLines(items) {
      const seen = new Set();
      const output = [];
      for (const item of items || []) {
        const value = String(item || "").trim();
        if (!value || seen.has(value)) continue;
        seen.add(value);
        output.push(value);
      }
      return output;
    },

    splitLines(text) {
      return String(text || "")
        .split(/\r?\n/)
        .map((item) => item.trim())
        .filter(Boolean);
    },
  };
}
