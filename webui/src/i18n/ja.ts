import type { TranslationKey } from "./index.ts";

// Japanese dictionary. Mirrors the key order of en.ts so future diffs against
// the English source line up. Typed as `Record<TranslationKey, string>` so a
// missing or extra key fails typecheck (WEBUI_SPEC.md Section 7.5). Register:
// operations-console short form (da/dearu mixed with concise noun phrases);
// technical terms (RCON, JAR, Worker, EULA, ZIP, UUID, MiB) kept as-is.
export const ja: Record<TranslationKey, string> = {
  "app.title": "mc-server-dashboard",

  // Shell chrome
  "shell.brand": "MC Dashboard",
  "shell.account": "アカウント",
  "shell.switchCommunity": "コミュニティを切り替え",
  "shell.noCommunity": "コミュニティなし",
  "shell.noCommunities": "まだどのコミュニティにも所属していません。",
  "shell.language": "言語",
  "shell.language.en": "English",
  "shell.language.ja": "日本語",

  // Sidebar navigation (WEBUI_SPEC.md Section 5)
  "nav.community": "コミュニティ",
  "nav.dashboard": "ダッシュボード",
  "nav.createServer": "サーバーを作成",
  "nav.settings": "コミュニティ設定",
  "nav.admin": "プラットフォーム管理",
  "nav.adminOverview": "概要",
  "nav.adminUsers": "ユーザー",
  "nav.adminCommunities": "コミュニティ",
  "nav.adminWorkers": "Worker",
  "nav.adminVersions": "バージョンとJAR",
  "nav.adminAudit": "全体監査ログ",
  "nav.sharedResources": "共有リソース",

  // Placeholder pages (Phase 1: routing skeleton only)
  "page.dashboard": "サーバー",
  "page.serverCreate": "サーバーを作成",
  "page.account": "アカウント",
  "page.adminOverview": "プラットフォーム概要",
  "page.adminUsers": "ユーザー管理",
  "page.adminCommunities": "コミュニティ",
  "page.adminWorkers": "Worker",
  "page.adminVersions": "バージョンとJAR",
  "page.adminAudit": "全体監査ログ",
  "page.placeholder":
    "プレースホルダーページ — 内容は後のフェーズで追加されます。",
  "page.notFound": "ページが見つかりません",

  // Not-found (404) route: shown for any unmatched URL (#639). The body
  // explains the state and the link returns the user to the landing route.
  "notFound.body": "お探しのページは存在しないか、移動されました。",
  "notFound.home": "ホームに戻る",

  // No-community empty state (#584): shown on the landing route when the
  // signed-in account belongs to zero communities.
  "noCommunity.title": "コミュニティがありません",
  "noCommunity.body":
    "このアカウントはまだどのコミュニティにも所属していません。コミュニティ単位でサーバー・メンバー・設定が管理されます。",
  "noCommunity.memberHint":
    "コミュニティに追加してもらうには、プラットフォーム管理者に依頼してください。",
  "noCommunity.adminHint":
    "プラットフォーム管理者は、最初のコミュニティを作成できます。",
  "noCommunity.adminCta": "コミュニティを作成",

  // Community-not-found state (#784): shown when a URL `:cid` is not one the
  // signed-in account belongs to (stale bookmark, or a community the user has
  // left). The dashboard and server-create pages derive their community from the
  // URL, so they render this instead of silently falling back to another one.
  "community.notFound.title": "コミュニティが見つかりません",
  "community.notFound.body":
    "このコミュニティは存在しないか、あなたはメンバーではありません。",

  // Auth links
  "auth.toRegister": "アカウントがない場合は登録",
  "auth.toLogin": "アカウントがある場合はサインイン",

  // Login / register pages and route guards (issue #410).
  "auth.loading": "読み込み中…",
  "auth.fieldUsername": "ユーザー名",
  "auth.fieldEmail": "メールアドレス",
  "auth.fieldPassword": "パスワード",
  "login.usernamePlaceholder": "ユーザー名",
  "login.submit": "サインイン",
  "login.submitting": "サインインしています…",
  "login.invalidCredentials": "ユーザー名またはパスワードが正しくありません。",
  "login.genericError": "サインインできませんでした。もう一度お試しください。",
  "login.sessionExpired":
    "セッションの有効期限が切れました。もう一度サインインしてください。",
  "register.usernamePlaceholder": "ユーザー名",
  "register.emailPlaceholder": "you@example.com",
  "register.passwordPlaceholder": "12文字以上",
  "register.confirmPassword": "パスワードの確認",
  "register.passwordHint":
    "12文字以上。ユーザー名やメールアドレスを含めることはできません。よくあるパスワードは拒否されます。",
  "register.submit": "アカウントを作成",
  "register.submitting": "アカウントを作成しています…",
  "register.success": "アカウントを作成しました。サインインしてください。",
  "register.genericError":
    "アカウントを作成できませんでした。もう一度お試しください。",
  "register.errPasswordMismatch": "パスワードが一致しません。",
  // Server-authoritative reason codes (AUTH_API.md 2; users.py register).
  "register.reason.too_short": "パスワードが短すぎます。",
  "register.reason.too_long_for_bcrypt": "パスワードが長すぎます。",
  "register.reason.insufficient_complexity":
    "パスワードが単純すぎます。より長く、または多様な文字を使ってください。",
  "register.reason.common_password":
    "よく使われるパスワードです。別のものにしてください。",
  "register.reason.contains_user_info":
    "パスワードにユーザー名やメールアドレスを含めることはできません。",
  "register.reason.simple_pattern":
    "パスワードが単純すぎるか、よくあるパターンです。",
  "register.reason.username_taken": "そのユーザー名はすでに使用されています。",
  "register.reason.email_taken": "そのメールアドレスはすでに登録されています。",
  "register.reason.invalid_username": "そのユーザー名は使用できません。",
  "register.reason.invalid_email": "有効なメールアドレスを入力してください。",

  // UX primitives (WEBUI_SPEC.md Section 7.4)
  "common.cancel": "キャンセル",
  "common.close": "閉じる",
  "common.showPassword": "パスワードを表示",
  "common.hidePassword": "パスワードを非表示",
  "common.resizeColumn": "ドラッグで幅を変更、ダブルクリックでリセット",
  "common.chooseFile": "ファイルを選択",
  "common.noFileChosen": "ファイルが選択されていません",

  // Upload progress (issue #1207).
  "upload.label": "アップロード中",
  "upload.bytes": "{loaded} / {total}",
  "upload.percent": "{percent}%",
  "upload.elapsed": "{seconds}秒経過",

  // Account page (WEBUI_SPEC.md 6.11)
  "account.subtitle": "プロフィール・セキュリティ・所属コミュニティの管理。",
  "account.signOut": "サインアウト",
  "account.loading": "読み込み中…",
  "account.loadError":
    "アカウントを読み込めませんでした。更新してみてください。",

  "account.profile.heading": "プロフィール",
  "account.profile.username": "ユーザー名",
  "account.profile.email": "メールアドレス",
  "account.profile.save": "プロフィールを保存",
  "account.profile.saved": "プロフィールを更新しました。",

  "account.password.heading": "パスワード",
  "account.password.current": "現在のパスワード",
  "account.password.new": "新しいパスワード",
  "account.password.confirm": "新しいパスワードの確認",
  "account.password.change": "パスワードを変更",
  "account.password.changed": "パスワードを変更しました。",
  "account.password.mismatch": "新しいパスワードが一致しません。",
  "account.password.hint":
    "12文字以上。ユーザー名、メールアドレス、単純なパターンは避けてください。",

  "account.memberships.heading": "メンバーシップ",
  "account.memberships.community": "コミュニティ",
  "account.memberships.none": "まだどのコミュニティにも所属していません。",
  "account.memberships.loadError":
    "メンバーシップを読み込めませんでした。更新してみてください。",

  "account.delete.heading": "危険な操作",
  "account.delete.label": "アカウントを削除",
  "account.delete.desc":
    "アカウントとすべてのメンバーシップを削除します。所有しているコミュニティは先に譲渡または削除する必要があります。",
  "account.delete.open": "削除…",
  "account.delete.dialogTitle": "アカウントを削除",
  "account.delete.dialogBody":
    "アカウントを完全に削除します。この操作は元に戻せません。確認のためユーザー名とパスワードを入力してください。",
  "account.delete.confirm": "アカウントを削除",
  "account.delete.prompt": "削除を有効にするにはユーザー名を入力",
  "account.delete.password": "確認のためパスワードを入力",

  // API reason codes (RFC 9457 `reason`) surfaced inline / via toast.
  "account.error.username_taken": "そのユーザー名はすでに使用されています。",
  "account.error.email_taken": "そのメールアドレスはすでに使用されています。",
  "account.error.invalid_username": "そのユーザー名は無効です。",
  "account.error.invalid_email": "そのメールアドレスは無効です。",
  "account.error.invalid_credentials": "現在のパスワードが正しくありません。",
  "account.error.too_short": "パスワードが短すぎます。",
  "account.error.too_long": "パスワードが長すぎます。",
  "account.error.too_long_for_bcrypt": "パスワードが長すぎます。",
  "account.error.insufficient_complexity":
    "パスワードが単純すぎます — 文字種を混ぜるか、より長くしてください。",
  "account.error.common_password":
    "そのパスワードはよく使われています。別のものにしてください。",
  "account.error.contains_user_info":
    "パスワードにユーザー名やメールアドレスを含めることはできません。",
  "account.error.simple_pattern":
    "同じ文字の繰り返しや連続した並びは避けてください。",
  "account.error.owns_community":
    "アカウントを削除する前に、所有しているコミュニティを譲渡または削除してください。",
  "account.error.last_platform_admin":
    "あなたは最後のプラットフォーム管理者のため、アカウントを削除できません。",
  "account.error.generic": "問題が発生しました。もう一度お試しください。",

  // Dashboard server cards (WEBUI_SPEC.md 6.2).
  "dashboard.subtitle": "このコミュニティのサーバー。",
  "dashboard.loading": "サーバーを読み込んでいます…",
  "dashboard.loadError":
    "サーバーを読み込めませんでした。更新してみてください。",
  "dashboard.empty": "まだサーバーがありません。",
  "dashboard.emptyHint": "最初のサーバーを作成して始めましょう。",
  "dashboard.createServer": "サーバーを作成",
  "dashboard.noWorker": "ホスト未割り当て",
  "dashboard.start": "起動",
  "dashboard.startCrashed": "再起動",
  "dashboard.stop": "停止",
  "dashboard.restart": "再起動",
  // Card/table view toggle (#541); cards remain the default.
  "dashboard.view.label": "サーバー一覧の表示",
  "dashboard.view.cards": "カード",
  "dashboard.view.table": "テーブル",
  // Table-view column headers (#541): the same data as the cards.
  "dashboard.col.name": "名前",
  "dashboard.col.state": "状態",
  "dashboard.col.type": "種類 / バージョン",
  "dashboard.col.backend": "バックエンド",
  "dashboard.col.port": "ポート",
  "dashboard.col.address": "アドレス",
  "dashboard.col.worker": "ホスト",
  "dashboard.col.actions": "操作",
  // Observed-state pill labels (WEBUI_SPEC.md 2.3).
  "dashboard.state.starting": "起動中",
  "dashboard.state.running": "稼働中",
  "dashboard.state.stopping": "停止中",
  "dashboard.state.stopped": "停止済み",
  "dashboard.state.restarting": "再起動中",
  "dashboard.state.crashed": "クラッシュ",
  "dashboard.state.unknown": "不明",
  // Lifecycle action feedback.
  "dashboard.actionFailed":
    "操作を完了できませんでした。もう一度お試しください。",
  // Conflict-flavoured (server_unsettled-style) lifecycle races (SPEC 7.4).
  "dashboard.stateChanged": "状態が変化しました — 更新しました。",
  // Sanitized 409 start-failure reasons (issue #225).
  "dashboard.lifecycle.portConflict":
    "起動できませんでした: ポートがすでに使用されています。",
  "dashboard.lifecycle.imageMissing":
    "起動できませんでした: サーバーのファイルが準備できていません。しばらくしてから再試行してください。",
  // 503 service-unavailable reasons (issue #1092).
  "dashboard.lifecycle.noEligibleWorker":
    "現在利用可能なサーバーホストがありません。システムが起動中の場合は、しばらく待ってから再試行してください。",
  "dashboard.lifecycle.workerUnavailable":
    "サーバーホストとの通信に失敗しました。しばらく待ってから再試行してください。",
  "dashboard.lifecycle.jarUnavailable":
    "サーバーファイルを準備できませんでした。しばらく待ってから再試行してください。",
  // Live-status degraded indicator: WS down, polling fallback (SPEC 6.2 / 7.2).
  "dashboard.liveDegraded": "再接続中 — 表示の更新が遅れることがあります",
  // Clickable join-hostname copy feedback.
  "dashboard.copiedJoinHostname": "コピーしました！",
  // Filter and sort controls (#1123).
  "dashboard.filter.search": "名前で検索…",
  "dashboard.filter.state": "状態でフィルター",
  "dashboard.filter.noMatch": "フィルター条件に一致するサーバーがありません。",
  "dashboard.sort.label": "並べ替え",
  "dashboard.sort.name": "名前",
  "dashboard.sort.state": "状態",
  "dashboard.sort.type": "種類",

  // Server detail page (WEBUI_SPEC.md 6.4 / 6.9).
  "serverDetail.loading": "サーバーを読み込んでいます…",
  "serverDetail.loadError":
    "このサーバーを読み込めませんでした。更新してみてください。",
  "serverDetail.breadcrumb": "サーバー",
  // Overview header.
  "serverDetail.crashDetail": "クラッシュ理由:",
  "serverDetail.crashBanner.guidance":
    "サーバーがクラッシュしました。再起動をクリックして再試行するか、コンソールで詳細を確認してください。",
  "serverDetail.crashBanner.viewConsole": "コンソールを表示",
  "serverDetail.converging": "反映中…",
  "serverDetail.noWorker": "ホスト未割り当て",
  "serverDetail.worker": "ホスト",
  "serverDetail.noPort": "ポートなし",
  // Relay join hostname (issue #961).
  "serverDetail.copiedJoinHostname": "コピーしました！",
  // Tabs (WEBUI_SPEC.md 6.4–6.9).
  "serverDetail.tab.overview": "概要",
  "serverDetail.tab.console": "コンソール",
  "serverDetail.tab.files": "ファイル",
  "serverDetail.tab.backups": "バックアップ",
  "serverDetail.tab.players": "プレイヤー",
  "serverDetail.tab.settings": "設定",
  // Overview live metrics strip + log tail (issue #440, WEBUI_SPEC.md 6.4).
  "serverDetail.metric.cpu": "CPU",
  "serverDetail.metric.memory": "メモリ",
  "serverDetail.metric.players": "プレイヤー",
  "serverDetail.metric.cores": "コア",
  "serverDetail.metric.mib": "MiB",
  // Before the first metrics frame arrives, vs. no stream while not running.
  "serverDetail.metric.collecting": "収集中…",
  "serverDetail.metric.idle": "停止中はメトリクスがありません",
  "serverDetail.logTailHeading": "最近のログ",
  "serverDetail.openConsole": "コンソールを開く",
  "serverDetail.logTailEmpty": "まだログ出力がありません。",
  // Inline divider where the client fell behind and missed events (SPEC 7.2).
  "serverDetail.missedEvents": "— 欠落したイベント —",
  // Console tab (issue #440, WEBUI_SPEC.md 6.5).
  "serverDetail.console.follow": "自動スクロール",
  "serverDetail.console.filter": "絞り込み…",
  "serverDetail.console.clear": "クリア",
  "serverDetail.console.send": "送信",
  "serverDetail.console.commandPlaceholder": "コマンドを入力…",
  "serverDetail.console.notRunning":
    "コマンドはサーバーの稼働中のみ使用できます。",
  "serverDetail.commandFailed": "コマンドが失敗しました。",
  // Lifecycle controls.
  "serverDetail.start": "起動",
  "serverDetail.startCrashed": "再起動",
  "serverDetail.stop": "停止",
  "serverDetail.stopGraceful": "停止（通常）",
  "serverDetail.stopForce": "強制停止",
  "serverDetail.forceStop.dialogTitle": "サーバーを強制停止しますか？",
  "serverDetail.forceStop.dialogBody":
    "強制停止はサーバープロセスを即座に終了します。グレースフルシャットダウンは行われず、未保存のデータ（チャンク、プレイヤーの進行状況）が失われる可能性があります。",
  "serverDetail.forceStop.confirm": "強制停止",
  "serverDetail.restart": "再起動",
  "serverDetail.export": "エクスポート",
  "serverDetail.delete": "削除",
  // Settings tab (WEBUI_SPEC.md 6.9).
  "serverDetail.settings.general": "全般",
  "serverDetail.settings.name": "サーバー名",
  "serverDetail.settings.gamePort": "ゲームポート",
  "serverDetail.settings.executionBackend": "実行バックエンド",
  "serverDetail.settings.executionBackendHint": "作成後は変更できません。",
  "serverDetail.settings.config": "設定の上書き",
  "serverDetail.settings.configKey": "キー",
  "serverDetail.settings.configValue": "値",
  "serverDetail.settings.configAdd": "上書きを追加",
  "serverDetail.settings.configRemove": "削除",
  "serverDetail.settings.configHint":
    "値はJSONとして解釈されます: 12 は数値、true は真偽値、それ以外は文字列です。",
  // Per-server memory limit (issue #709).
  "serverDetail.settings.memoryLimit": "メモリ上限（MiB）",
  "serverDetail.settings.memoryLimitDefault": "既定値",
  "serverDetail.settings.memoryLimitHint":
    "このサーバーに割り当てるメモリの上限（MiB）。空欄にすると既定値を使用します。",
  "serverDetail.settings.memoryLimitRange":
    "512〜1048576 MiB の整数を入力するか、空欄にして既定値を使用してください。",
  // Per-server CPU allocation (issue #726).
  "serverDetail.settings.cpuAllocation": "CPU割り当て（ミリコア）",
  "serverDetail.settings.cpuAllocationDefault": "自動",
  "serverDetail.settings.cpuAllocationHint":
    "このサーバーへのCPUの割り当て（ミリコア、1000＝1コア）。上限ではなく負荷時の相対的な割り当てで、ホストに余裕があればこれを超えて使用できます。空欄にすると自動になります。",
  "serverDetail.settings.cpuAllocationRange":
    "100〜128000 ミリコアの整数を入力するか、空欄にして自動にしてください。",
  // Relay join address name field (issue #961).
  "serverDetail.settings.slug": "参加アドレス名",
  "serverDetail.settings.slugHint":
    "小文字・数字・ハイフンのみ使用できます。先頭と末尾にハイフンは使えません。変更しなければ現在の参加アドレスが維持されます。",
  "serverDetail.settings.slugInvalid":
    "有効なDNSラベルを入力してください: 小文字・数字・ハイフンのみ、先頭と末尾にハイフン不可。",
  "serverDetail.settings.slugTaken":
    "この参加アドレス名はすでに使用されています。",
  "serverDetail.settings.save": "変更を保存",
  "serverDetail.settings.saved": "設定を保存しました。",
  "serverDetail.settings.atRestHint":
    "名前、ゲームポート、設定の変更にはサーバーの停止が必要です。",
  // On-blur game-port availability check (GET /ports/check/{port}).
  "serverDetail.port.available": "✓ 利用可能",
  "serverDetail.port.current": "✓ 利用可能（現在）",
  "serverDetail.port.taken": "ポートはすでに使用されています。",
  "serverDetail.port.outOfRange": "ポートが許可された範囲外です。",
  "serverDetail.port.checkError": "ポートの空き状況を確認できませんでした。",
  // Danger zone.
  "serverDetail.danger.heading": "危険な操作",
  "serverDetail.danger.exportTitle": "サーバーをエクスポート",
  "serverDetail.danger.exportDesc":
    "サーバーのすべてのファイルをZIPアーカイブとしてダウンロードします。",
  "serverDetail.danger.exportButton": "ZIPをエクスポート",
  "serverDetail.danger.deleteTitle": "サーバーを削除",
  "serverDetail.danger.deleteDesc":
    "サーバー、そのデータおよびバックアップを完全に削除します。",
  "serverDetail.danger.deleteButton": "削除…",
  "serverDetail.delete.dialogTitle": "サーバーを削除",
  "serverDetail.delete.dialogBody":
    "サーバー、そのデータおよびバックアップを完全に削除します。この操作は元に戻せません。確認のためサーバー名を入力してください。",
  "serverDetail.delete.confirm": "サーバーを削除",
  "serverDetail.delete.prompt": "削除を有効にするにはサーバー名を入力",
  // Outcomes (toasts).
  "serverDetail.exportStarted": "エクスポートのダウンロードを開始しました。",
  "serverDetail.deleted": "サーバーを削除しました。",
  "serverDetail.error.notStopped":
    "この変更を行う前にサーバーを停止してください。",
  "serverDetail.error.unsettled":
    "エクスポートの前にサーバーを停止する必要があります。",
  "serverDetail.error.portTaken": "そのゲームポートはすでに使用されています。",
  "serverDetail.error.portOutOfRange":
    "そのゲームポートは許可された範囲外です。",
  "serverDetail.error.invalidSnapshotInterval":
    "snapshot_interval_seconds は、設定された下限以上の整数の秒数である必要があります。",
  "serverDetail.error.invalidBackupSchedule":
    "backup_interval_hours は1以上の整数の時間数である必要があります。",
  "serverDetail.error.invalidMemoryLimit":
    "メモリ上限は 512〜1048576 MiB の整数である必要があります。",
  "serverDetail.error.invalidCpuAllocation":
    "CPU割り当ては 100〜128000 ミリコアの整数である必要があります。",
  // Relay join address name errors (issue #961).
  "serverDetail.error.invalidSlug":
    "参加アドレス名は有効なDNSラベルである必要があります: 小文字・数字・ハイフンのみ、先頭と末尾にハイフン不可。",
  "serverDetail.error.slugTaken":
    "その参加アドレス名はすでに使用されています。",
  "serverDetail.error.generic": "問題が発生しました。もう一度お試しください。",
  // EULA acceptance dialog (issue #1104).
  "serverDetail.eulaDialog.title": "Minecraft EULAに同意しますか？",
  "serverDetail.eulaDialog.body":
    "このサーバーを起動するには、Minecraftエンドユーザーライセンス契約（EULA）に同意する必要があります。",
  "serverDetail.eulaDialog.accept": "同意して起動",
  "serverDetail.eulaDialog.link": "Minecraft EULAを確認する",

  // Backups tab (WEBUI_SPEC.md 6.7).
  "backups.loading": "バックアップを読み込んでいます…",
  "backups.loadError": "バックアップを読み込めませんでした。",
  "backups.noRead": "バックアップを表示する権限がありません。",
  "backups.none": "—",
  "backups.empty": "まだバックアップがありません。",
  // Stats header.
  "backups.stat.count": "バックアップ数",
  "backups.stat.totalSize": "合計サイズ",
  // Shown beside the total when some backups have no recorded size (legacy
  // NULL-size rows, #281): the figure sums only the known sizes (#640).
  "backups.stat.totalSizePartial": "判明分のみ",
  "backups.stat.newest": "最新",
  "backups.stat.oldest": "最古",
  // Table.
  "backups.col.created": "作成日時",
  "backups.col.source": "作成方法",
  "backups.col.condition": "状態",
  "backups.col.size": "サイズ",
  "backups.col.creator": "作成者",
  "backups.unknownSize": "不明",
  "backups.unknownCreator": "—",
  // Condition badge (API `health`: healthy / quarantined / unknown). Plain
  // language — no internal jargon. A healthy backup shows nothing, keeping the
  // row quiet; only the at-risk states are flagged.
  "backups.health.quarantined": "破損",
  "backups.health.quarantinedTitle":
    "このバックアップのデータは破損していることが判明しています。復元するとワールドが壊れる可能性があります。",
  "backups.health.unknown": "未検証",
  "backups.health.unknownTitle":
    "このバックアップは検査されていないため、状態は不明です。",
  // Actions.
  "backups.create": "+ バックアップを作成",
  "backups.upload": "アップロード",
  "backups.download": "ダウンロード",
  "backups.restore": "復元",
  "backups.delete": "削除",
  // Schedule field (backup_interval_hours on the server config blob).
  "backups.schedule.label": "スケジュール: 毎",
  "backups.schedule.unit": "時間",
  "backups.schedule.save": "保存",
  "backups.schedule.saved": "バックアップスケジュールを保存しました。",
  // Restore dialog (stopped-only; two-step stop-then-restore).
  "backups.restoreDialog.title": "バックアップを復元",
  "backups.restoreDialog.blocked":
    "復元するとサーバーのデータが上書きされるため、サーバーを停止する必要があります。",
  "backups.restoreDialog.blockedHint":
    "サーバーを停止してから、このダイアログを再度開いて復元を確定してください。",
  "backups.restoreDialog.blockedNoStop":
    "オペレーターにサーバーの停止を依頼してから、このダイアログを再度開いて復元を確定してください。",
  "backups.restoreDialog.stop": "サーバーを停止",
  "backups.restoreDialog.stopping": "サーバーを停止しています…",
  "backups.restoreDialog.body":
    "サーバーの現在のデータをこのバックアップで上書きします。この操作は元に戻せません。",
  "backups.restoreDialog.prompt": "確認のため RESTORE と入力",
  "backups.restoreDialog.phrase": "RESTORE",
  "backups.restoreDialog.confirm": "バックアップを復元",
  // Force-restore warning shown only when the chosen backup is quarantined
  // (health === "quarantined"). It restores anyway with force=true, so the copy
  // makes the deliberate, damaged-data nature explicit (#745).
  "backups.restoreDialog.damagedWarning":
    "このバックアップのデータは破損していることが判明しています。復元するとサーバーのワールドが壊れた状態になる可能性があり、その後修復する方法はありません。",
  "backups.restoreDialog.damagedConfirm": "破損したバックアップをそれでも復元",
  // Acknowledgement checkbox label gating the force-restore — affirmation phrased
  // (the user asserts they accept the risk), not a restatement of the warning.
  "backups.restoreDialog.damagedAck":
    "このバックアップが破損しており、修復できない壊れたワールドになる可能性があることを理解しました。",
  // Delete dialog (typed confirm).
  "backups.deleteDialog.title": "バックアップを削除",
  "backups.deleteDialog.body":
    "バックアップアーカイブを完全に削除します。この操作は元に戻せません。",
  "backups.deleteDialog.prompt": "確認のため DELETE と入力",
  "backups.deleteDialog.phrase": "DELETE",
  "backups.deleteDialog.confirm": "バックアップを削除",
  // Outcomes (toasts).
  "backups.created": "バックアップを作成しました。",
  "backups.uploaded": "バックアップをアップロードしました。",
  "backups.deleted": "バックアップを削除しました。",
  "backups.restored": "バックアップを復元しました。",
  "backups.error.notStopped":
    "バックアップを復元する前にサーバーを停止してください。",
  "backups.error.unsettled":
    "サーバーが起動／停止の途中です。完全に停止または稼働してから再度お試しください。",
  "backups.error.invalidArchive":
    "そのファイルは有効なバックアップアーカイブではありません。",
  "backups.error.workerUnavailable":
    "現在、バックアップを取得できるサーバーホストがありません。",
  "backups.error.invalidSchedule":
    "backup_interval_hours は1以上の整数の時間数である必要があります。",
  "backups.error.tooLarge":
    "ファイルサイズが512 MiBのアップロード上限を超えています。",
  "backups.error.generic": "問題が発生しました。もう一度お試しください。",

  // Files tab (WEBUI_SPEC.md 6.6).
  "files.denied": "このサーバーのファイルを表示する権限がありません。",
  "files.runningNotice":
    "サーバーは稼働中です — ファイルの編集は稼働中のサーバーに即座に反映されます。アップロードとフォルダの作成にはサーバーを停止する必要があります。",
  "files.root": "ルート",
  "files.loading": "読み込み中…",
  "files.listError":
    "このディレクトリを一覧表示できませんでした。更新してみてください。",
  "files.openError": "このファイルを開けませんでした。",
  "files.empty": "このディレクトリは空です。",
  "files.noSelection": "表示または編集するファイルを選択してください。",
  "files.truncated":
    "一覧が省略されました — エントリが多すぎてすべては表示できません。",
  "files.binary": "バイナリファイル — 表示するにはダウンロードしてください。",
  "files.editorLabel": "ファイルの内容",
  "files.upload": "アップロード",
  "files.extractZip": "ZIPを展開",
  "files.newFolder": "新しいフォルダ",
  "files.folderName": "フォルダ名",
  "files.newName": "新しい名前",
  "files.create": "作成",
  "files.rename": "名前を変更",
  "files.delete": "削除",
  "files.download": "ダウンロード",
  "files.save": "保存",
  "files.delete.dialogTitle": "ファイルを削除",
  "files.delete.dialogBody":
    "選択したファイルまたはディレクトリを完全に削除します。確認のためその名前を入力してください。",
  "files.delete.confirm": "完全に削除",
  // Search (files/search).
  "files.search.label": "ファイルを検索",
  "files.search.placeholder": "名前で検索…",
  "files.search.byName": "名前",
  "files.search.byContent": "内容",
  "files.search.submit": "検索",
  "files.search.empty": "一致するファイルはありませんでした。",
  "files.search.truncated":
    "最初の結果を表示しています — さらに見るには検索を絞り込んでください。",
  "files.search.error": "検索に失敗しました。もう一度お試しください。",
  // History drawer + rollback (files/history, files/rollback).
  "files.history": "履歴",
  "files.history.title": "バージョン履歴",
  "files.history.loading": "バージョンを読み込んでいます…",
  "files.history.error": "バージョン履歴を読み込めませんでした。",
  "files.history.empty": "以前のバージョンはまだ保存されていません。",
  "files.history.hint":
    "最新のバージョンのみが保持され（既定で10件）、古いものは破棄されます。",
  "files.history.rollback": "ロールバック",
  "files.history.close": "閉じる",
  "files.rollback.dialogTitle": "ファイルをロールバック",
  "files.rollback.dialogBody":
    "現在のファイルを選択したバージョンで置き換えます。サーバーを停止する必要があります。",
  "files.rollback.confirm": "今すぐロールバック",
  // Outcomes (toasts).
  "files.saved": "ファイルを保存しました。",
  "files.uploaded": "アップロードが完了しました。",
  "files.folderCreated": "フォルダを作成しました。",
  "files.renamed": "名前を変更しました。",
  "files.deleted": "削除しました。",
  "files.rolledBack": "選択したバージョンにロールバックしました。",
  "files.error.serverMustBeStopped":
    "ファイルのアップロードやフォルダの作成を行う前にサーバーを停止してください。",
  "files.error.contentDirProtected":
    "このディレクトリは{noun}タブで管理されています。コンテンツの追加・削除は{noun}タブをご利用ください。",
  "files.error.goToContentTab": "{noun}タブへ移動",
  "files.error.tooLarge":
    "ファイルサイズが512 MiBのアップロード上限を超えています。",
  "files.error.generic": "問題が発生しました。もう一度お試しください。",

  // Players tab — applied op/whitelist groups (issue #453, WEBUI_SPEC.md 6.8).
  "players.heading": "適用中のグループ",
  "players.loading": "グループを読み込んでいます…",
  "players.loadError": "グループを読み込めませんでした。更新してみてください。",
  "players.empty": "このサーバーにはまだグループが適用されていません。",
  "players.kind.op": "op",
  "players.kind.whitelist": "whitelist",
  // Member count shown next to each group (the group's player list length).
  "players.memberCount": "人",
  "players.detach": "解除",
  "players.detached": "グループを解除しました。",
  // Apply picker: community groups not yet applied to this server.
  "players.attachHeading": "グループを適用",
  "players.attachEmpty": "このコミュニティのグループはすべて適用済みです。",
  // Distinct from attachEmpty: the community has no groups at all (issue #642).
  "players.attachNoGroups": "このコミュニティにはまだグループがありません。",
  "players.attach": "適用",
  "players.attached": "グループを適用しました。",
  // Inline pointer to the full Groups management surface (Phase 6).
  "players.manageHint": "グループの作成と編集はコミュニティ設定で行えます。",
  "players.manageLink": "コミュニティ設定",
  "players.error.generic": "問題が発生しました。もう一度お試しください。",

  // Sessions view — relay game session history (issue #961).
  "sessions.heading": "セッション",
  "sessions.loading": "セッションを読み込んでいます…",
  "sessions.loadError":
    "セッションを読み込めませんでした。更新してみてください。",
  "sessions.empty": "まだセッションの記録はありません。",
  "sessions.col.hostname": "ホスト名",
  "sessions.col.playerIp": "IPアドレス（申告値）",
  "sessions.col.username": "ユーザー名（申告値）",
  "sessions.col.start": "開始",
  "sessions.col.end": "終了",
  "sessions.valueUnknown": "—",
  "sessions.active": "アクティブ",
  "sessions.prev": "前へ",
  "sessions.next": "次へ",

  // Server create wizard (WEBUI_SPEC.md 6.3).
  "serverCreate.subtitle": "新しいMinecraftサーバーを作成します。",
  "serverCreate.denied": "サーバーを作成する権限がありません。",
  "serverCreate.tab.new": "新規サーバー",
  "serverCreate.tab.import": "ZIPをインポート",
  // Wizard step rail.
  "serverCreate.step.type": "種類とバージョン",
  "serverCreate.step.runtime": "ランタイム",
  "serverCreate.step.config": "設定とEULA",
  "serverCreate.next": "次へ",
  "serverCreate.back": "戻る",
  "serverCreate.create": "サーバーを作成",
  "serverCreate.creating": "作成しています…",
  // Step 1 — type & version.
  "serverCreate.typeHeading": "サーバーの種類",
  "serverCreate.typeLoading": "サーバーの種類を読み込んでいます…",
  "serverCreate.typeLoadError":
    "バージョンカタログを読み込めませんでした。更新してみてください。",
  "serverCreate.versionLabel": "Minecraftバージョン",
  "serverCreate.versionLoading": "バージョンを読み込んでいます…",
  "serverCreate.versionLoadError":
    "この種類のバージョンを読み込めませんでした。",
  "serverCreate.spigotHint":
    "公式の配布APIがありません — 代わりにPaperを使用してください。",
  "serverCreate.type.vanilla": "Vanilla",
  "serverCreate.type.paper": "Paper",
  "serverCreate.type.fabric": "Fabric",
  "serverCreate.type.forge": "Forge",
  "serverCreate.type.spigot": "Spigot",
  "serverCreate.typeSub.vanilla": "公式",
  "serverCreate.typeSub.paper": "高性能フォーク",
  "serverCreate.typeSub.fabric": "軽量Mod",
  "serverCreate.typeSub.forge": "本格Mod",
  "serverCreate.typeSub.spigot": "非対応",
  // Step 2 — runtime.
  "serverCreate.backendLabel": "実行バックエンド",
  "serverCreate.backend.container": "コンテナ",
  "serverCreate.portLabel": "ゲームポート",
  "serverCreate.portHint": "空いている次のポートを自動入力しました。",
  "serverCreate.portChecking": "ポートの空き状況を確認しています…",
  "serverCreate.portAvailable": "ポートは利用可能です。",
  "serverCreate.portTaken": "ポートはすでに使用されています。",
  "serverCreate.portOutOfRange": "ポートが許可された範囲外です。",
  "serverCreate.portCheckFailed": "そのポートを確認できませんでした。",
  // Step 3 — config & EULA.
  "serverCreate.nameLabel": "サーバー名",
  "serverCreate.namePlaceholder": "survival",
  // Per-server resource allocation in the create wizard (issue #715).
  "serverCreate.memoryLimitLabel": "メモリ上限（MiB）",
  "serverCreate.memoryLimitDefault": "既定値",
  "serverCreate.memoryLimitHint":
    "このサーバーに割り当てるメモリの上限（MiB）。空欄にすると既定値を使用します。",
  "serverCreate.memoryLimitRange":
    "512〜1048576 MiB の整数を入力するか、空欄にして既定値を使用してください。",
  "serverCreate.cpuAllocationLabel": "CPU割り当て（ミリコア）",
  "serverCreate.cpuAllocationDefault": "自動",
  "serverCreate.cpuAllocationHint":
    "このサーバーへのCPUの割り当て（ミリコア、1000＝1コア）。上限ではなく負荷時の相対的な割り当てで、ホストに余裕があればこれを超えて使用できます。空欄にすると自動になります。",
  "serverCreate.cpuAllocationRange":
    "100〜128000 ミリコアの整数を入力するか、空欄にして自動にしてください。",
  // Optional join address name (slug) at create time (issue #981).
  "serverCreate.slugLabel": "参加アドレス名（任意）",
  "serverCreate.slugPlaceholder": "例: myserver",
  "serverCreate.slugHint":
    "小文字・数字・ハイフンのみ使用できます。空欄にするとランダムなアドレスが生成されます。",
  "serverCreate.slugInvalid":
    "有効なDNSラベルを入力してください：小文字・数字・ハイフンのみ、先頭・末尾にハイフン不可。",
  "serverCreate.propsHeading": "server.properties の上書き",
  "serverCreate.propsHint":
    "任意。初回起動時に server.properties に書き込まれるキー。",
  "serverCreate.propKeyPlaceholder": "キー（例: motd）",
  "serverCreate.propValuePlaceholder": "値",
  "serverCreate.propAdd": "上書きを追加",
  "serverCreate.propRemove": "削除",
  "serverCreate.eulaLabel": "Minecraft EULAに同意します。",
  "serverCreate.eulaWarning":
    "EULAに同意しない場合、サーバーは作成されますが、後で同意するまで起動できません。",
  // Create error surfacing.
  "serverCreate.error.spigot_unsupported":
    "Spigotは非対応です — 代わりにPaperを使用してください。",
  "serverCreate.error.port_taken":
    "そのゲームポートはすでに使用されています。別のポートを選択してください。",
  "serverCreate.error.port_out_of_range":
    "そのゲームポートは許可された範囲外です。",
  "serverCreate.error.server_name_exists":
    "その名前のサーバーはこのコミュニティにすでに存在します。",
  "serverCreate.error.invalid_server_name": "そのサーバー名は使用できません。",
  "serverCreate.error.unknown_version":
    "そのバージョンはこの種類では利用できません。",
  "serverCreate.error.invalid_memory_limit":
    "メモリ上限は 512〜1048576 MiB の整数で指定してください。",
  "serverCreate.error.invalid_cpu_allocation":
    "CPU割り当ては 100〜128000 ミリコアの整数で指定してください。",
  "serverCreate.error.invalid_slug":
    "その参加アドレス名は無効です。小文字・数字・ハイフンのみ使用できます。",
  "serverCreate.error.slug_taken":
    "その参加アドレス名はすでに使用されています。別の名前を選択してください。",
  "serverCreate.genericError":
    "サーバーを作成できませんでした。もう一度お試しください。",
  // Import tab.
  "serverCreate.import.heading": "ZIPエクスポートからインポート",
  "serverCreate.import.hint":
    "別のインスタンスからエクスポートしたZIPをアップロードします。下記の名前とバックエンドが適用されます。EULAは引き継がれません。",
  "serverCreate.import.fileLabel": "エクスポートアーカイブ（.zip）",
  "serverCreate.import.submit": "サーバーをインポート",
  "serverCreate.import.importing": "インポートしています…",
  "serverCreate.import.noFile": "インポートするZIPファイルを選択してください。",
  "serverCreate.import.error.invalid_export_metadata":
    "そのアーカイブは有効なサーバーエクスポートではありません。",
  "serverCreate.import.tooLarge":
    "そのアーカイブはインポートするには大きすぎます。",

  // Community settings (WEBUI_SPEC.md 6.10).
  "communitySettings.loading": "読み込み中…",
  "communitySettings.loadError":
    "このコミュニティを読み込めませんでした。更新してみてください。",
  "communitySettings.breadcrumb": "ダッシュボード",
  "communitySettings.tab.members": "メンバー",
  "communitySettings.tab.roles": "ロール",
  "communitySettings.tab.grants": "権限付与",
  "communitySettings.tab.groups": "グループ",
  "communitySettings.tab.audit": "監査ログ",
  "communitySettings.tab.general": "全般",

  // Members tab.
  "communitySettings.members.heading": "メンバー",
  "communitySettings.members.loading": "メンバーを読み込んでいます…",
  "communitySettings.members.loadError": "メンバーを読み込めませんでした。",
  "communitySettings.members.empty": "まだメンバーがいません。",
  "communitySettings.members.colUsername": "ユーザー名",
  "communitySettings.members.colRoles": "ロール",
  "communitySettings.members.unknownUser": "（不明なユーザー）",
  "communitySettings.members.add": "メンバーを追加…",
  "communitySettings.members.remove": "削除",
  "communitySettings.members.unassignRole": "ロールを解除",
  "communitySettings.members.assignRole": "ロールを割り当て",
  "communitySettings.members.noRolesLeft": "すべてのロールを割り当て済み。",
  "communitySettings.members.addDialogTitle": "メンバーを追加",
  "communitySettings.members.addDialogBody":
    "ユーザー名を正確に入力して、既存のユーザーをこのコミュニティに追加します。",
  "communitySettings.members.usernameLabel": "ユーザー名",
  "communitySettings.members.usernamePlaceholder": "ユーザー名",
  "communitySettings.members.addSubmit": "メンバーを追加",
  "communitySettings.members.addEmpty": "ユーザー名を入力してください。",
  "communitySettings.members.errUserNotFound":
    "そのユーザー名のユーザーはいません。",
  "communitySettings.members.errAlreadyMember":
    "そのユーザーはすでにこのコミュニティのメンバーです。",
  "communitySettings.members.errGeneric":
    "メンバーを追加できませんでした。もう一度お試しください。",
  "communitySettings.members.added": "メンバーを追加しました。",
  "communitySettings.members.removeDialogTitle": "メンバーを削除",
  "communitySettings.members.removeDialogBody":
    "このメンバーを削除すると、このコミュニティでのすべてのロールとサーバーごとの権限付与が取り消されます。この操作は元に戻せません。",
  "communitySettings.members.removeConfirm": "メンバーを削除",
  "communitySettings.members.removed": "メンバーを削除しました。",
  "communitySettings.members.removeError":
    "メンバーを削除できませんでした。もう一度お試しください。",
  "communitySettings.members.roleError":
    "ロールを更新できませんでした。もう一度お試しください。",

  // Audit tab (WEBUI_SPEC.md 6.10).
  "communitySettings.audit.heading": "監査ログ",
  "communitySettings.audit.loading": "監査ログを読み込んでいます…",
  "communitySettings.audit.loadError": "監査ログを読み込めませんでした。",
  "communitySettings.audit.empty": "一致する監査エントリがありません。",
  "communitySettings.audit.filterOperation": "操作",
  "communitySettings.audit.filterOperationPlaceholder": "例: server:start",
  "communitySettings.audit.filterActor": "実行者ID",
  "communitySettings.audit.filterActorPlaceholder": "ユーザーID",
  "communitySettings.audit.filterActorInvalid":
    "ユーザーID（UUID）である必要があります",
  "communitySettings.audit.filterSince": "開始",
  "communitySettings.audit.filterUntil": "終了",
  "communitySettings.audit.apply": "フィルターを適用",
  "communitySettings.audit.colTime": "時刻",
  "communitySettings.audit.colActor": "実行者",
  "communitySettings.audit.colOperation": "操作",
  "communitySettings.audit.colOutcome": "結果",
  "communitySettings.audit.colTarget": "対象",
  "communitySettings.audit.systemActor": "（システム）",
  "communitySettings.audit.prev": "前へ",
  "communitySettings.audit.next": "次へ",
  // 監査ログの operation コード（api audit/domain/operations.py の
  // `<resource>:<action>` 定数）と target_type の人間向けラベル。未対応のコードは
  // 生の値にフォールバックする（auditShared.tsx）ため、辞書に未登録のコードが
  // 来てもテーブルは壊れない（#643）。
  "communitySettings.audit.op.auth:login": "サインイン",
  "communitySettings.audit.op.auth:logout": "サインアウト",
  "communitySettings.audit.op.auth:register": "アカウント登録",
  "communitySettings.audit.op.auth:refresh": "セッション更新",
  "communitySettings.audit.op.auth:refresh_reuse": "更新トークンの再利用",
  "communitySettings.audit.op.auth:session_restore": "セッション復元",
  "communitySettings.audit.op.auth:password_change": "パスワード変更",
  "communitySettings.audit.op.auth:profile_update": "プロフィール更新",
  "communitySettings.audit.op.auth:account_delete": "アカウント削除",
  "communitySettings.audit.op.auth:session_revoke": "セッション失効",
  "communitySettings.audit.op.user:create": "ユーザー作成",
  "communitySettings.audit.op.user:deactivate": "ユーザー無効化",
  "communitySettings.audit.op.user:reactivate": "ユーザー再有効化",
  "communitySettings.audit.op.user:delete": "ユーザー削除",
  "communitySettings.audit.op.user:platform_admin_grant":
    "プラットフォーム管理者を付与",
  "communitySettings.audit.op.user:platform_admin_revoke":
    "プラットフォーム管理者を剥奪",
  "communitySettings.audit.op.community:provision": "コミュニティ作成",
  "communitySettings.audit.op.community:update": "コミュニティ更新",
  "communitySettings.audit.op.community:delete": "コミュニティ削除",
  "communitySettings.audit.op.member:add": "メンバー追加",
  "communitySettings.audit.op.member:remove": "メンバー削除",
  "communitySettings.audit.op.role:assign": "ロール割り当て",
  "communitySettings.audit.op.role:unassign": "ロール割り当て解除",
  "communitySettings.audit.op.role:create": "ロール作成",
  "communitySettings.audit.op.role:update": "ロール更新",
  "communitySettings.audit.op.role:delete": "ロール削除",
  "communitySettings.audit.op.grant:create": "権限付与",
  "communitySettings.audit.op.grant:revoke": "権限剥奪",
  "communitySettings.audit.op.server:create": "サーバー作成",
  "communitySettings.audit.op.server:update": "サーバー更新",
  "communitySettings.audit.op.server:delete": "サーバー削除",
  "communitySettings.audit.op.server:start": "サーバー起動",
  "communitySettings.audit.op.server:stop": "サーバー停止",
  "communitySettings.audit.op.server:restart": "サーバー再起動",
  "communitySettings.audit.op.server:command": "コンソールコマンド送信",
  "communitySettings.audit.op.server:export": "サーバーエクスポート",
  "communitySettings.audit.op.server:import": "サーバーインポート",
  "communitySettings.audit.op.backup:create": "バックアップ作成",
  "communitySettings.audit.op.backup:restore": "バックアップ復元",
  "communitySettings.audit.op.backup:delete": "バックアップ削除",
  "communitySettings.audit.op.backup:upload": "バックアップアップロード",
  "communitySettings.audit.op.backup:download": "バックアップダウンロード",
  "communitySettings.audit.op.file:write": "ファイル編集",
  "communitySettings.audit.op.file:rollback": "ファイルを元に戻す",
  "communitySettings.audit.op.file:upload": "ファイルアップロード",
  "communitySettings.audit.op.file:download": "ファイルダウンロード",
  "communitySettings.audit.op.file:rename": "ファイル名変更",
  "communitySettings.audit.op.file:delete": "ファイル削除",
  "communitySettings.audit.op.file:mkdir": "フォルダ作成",
  "communitySettings.audit.op.file:search": "ファイル検索",
  "communitySettings.audit.op.version:refresh": "バージョンカタログ更新",
  "communitySettings.audit.op.version:jar_gc": "ディスク容量を解放",
  "communitySettings.audit.op.worker:drain_set": "Workerをドレイン",
  "communitySettings.audit.op.worker:drain_clear": "Workerのドレイン解除",
  "communitySettings.audit.op.group:create": "プレイヤーグループ作成",
  "communitySettings.audit.op.group:update": "プレイヤーグループ更新",
  "communitySettings.audit.op.group:delete": "プレイヤーグループ削除",
  "communitySettings.audit.op.group:player_add": "グループにプレイヤー追加",
  "communitySettings.audit.op.group:player_remove":
    "グループからプレイヤー削除",
  "communitySettings.audit.op.group:attach": "プレイヤーグループを適用",
  "communitySettings.audit.op.group:detach": "プレイヤーグループを解除",
  "communitySettings.audit.targetType.community": "コミュニティ",
  "communitySettings.audit.targetType.user": "ユーザー",
  "communitySettings.audit.targetType.role": "ロール",
  "communitySettings.audit.targetType.grant": "権限付与",
  "communitySettings.audit.targetType.server": "サーバー",
  "communitySettings.audit.targetType.backup": "バックアップ",
  "communitySettings.audit.targetType.worker": "Worker",
  "communitySettings.audit.targetType.file": "ファイル",
  "communitySettings.audit.targetType.group": "プレイヤーグループ",

  // Roles tab.
  "communitySettings.roles.heading": "ロール",
  "communitySettings.roles.loading": "ロールを読み込んでいます…",
  "communitySettings.roles.loadError": "ロールを読み込めませんでした。",
  "communitySettings.roles.empty": "まだロールがありません。",
  "communitySettings.roles.create": "新規ロール…",
  "communitySettings.roles.preset": "プリセット",
  "communitySettings.roles.edit": "編集",
  "communitySettings.roles.delete": "削除",
  "communitySettings.roles.createDialogTitle": "新規ロール",
  "communitySettings.roles.editDialogTitle": "ロールを編集",
  "communitySettings.roles.nameLabel": "ロール名",
  "communitySettings.roles.namePlaceholder": "例: モデレーター",
  "communitySettings.roles.permissionsLabel": "権限",
  "communitySettings.roles.selectAll": "すべて選択",
  "communitySettings.roles.save": "ロールを保存",
  "communitySettings.roles.created": "ロールを作成しました。",
  "communitySettings.roles.updated": "ロールを更新しました。",
  "communitySettings.roles.deleted": "ロールを削除しました。",
  "communitySettings.roles.nameEmpty": "ロール名を入力してください。",
  "communitySettings.roles.errNameTaken": "その名前はすでに使用されています。",
  "communitySettings.roles.errInvalidName": "その名前は使用できません。",
  "communitySettings.roles.errPreset": "プリセットロールは変更できません。",
  "communitySettings.roles.errGeneric":
    "ロールを保存できませんでした。もう一度お試しください。",
  "communitySettings.roles.deleteError":
    "ロールを削除できませんでした。もう一度お試しください。",
  "communitySettings.roles.deleteDialogTitle": "ロールを削除",
  "communitySettings.roles.deleteDialogBody":
    "このロールを削除すると、保持しているすべてのメンバーから削除されます。この操作は元に戻せません。",
  "communitySettings.roles.deleteConfirm": "ロールを削除",
  // Permission family group labels (WEBUI_SPEC.md 2.2).
  "communitySettings.roles.family.server": "サーバー",
  "communitySettings.roles.family.file": "ファイル",
  "communitySettings.roles.family.backup": "バックアップ",
  "communitySettings.roles.family.member": "メンバー",
  "communitySettings.roles.family.role": "ロール",
  "communitySettings.roles.family.grant": "権限付与",
  "communitySettings.roles.family.group": "グループ",
  "communitySettings.roles.family.community": "コミュニティ",
  "communitySettings.roles.family.audit": "監査ログ",
  // Session family (issue #961).
  "communitySettings.roles.family.session": "セッション",
  // Permission code labels (the action within each family).
  "communitySettings.roles.code.server:create": "作成",
  "communitySettings.roles.code.server:read": "閲覧",
  "communitySettings.roles.code.server:update": "更新",
  "communitySettings.roles.code.server:delete": "削除",
  "communitySettings.roles.code.server:start": "起動",
  "communitySettings.roles.code.server:stop": "停止",
  "communitySettings.roles.code.server:restart": "再起動",
  "communitySettings.roles.code.server:command": "コマンド送信",
  "communitySettings.roles.code.file:read": "閲覧",
  "communitySettings.roles.code.file:edit": "編集",
  "communitySettings.roles.code.file:history": "履歴を表示",
  "communitySettings.roles.code.file:rollback": "ロールバック",
  "communitySettings.roles.code.backup:create": "作成",
  "communitySettings.roles.code.backup:read": "閲覧",
  "communitySettings.roles.code.backup:restore": "復元",
  "communitySettings.roles.code.backup:delete": "削除",
  "communitySettings.roles.code.backup:schedule": "スケジュール",
  "communitySettings.roles.code.member:read": "閲覧",
  "communitySettings.roles.code.member:add": "追加",
  "communitySettings.roles.code.member:remove": "削除",
  "communitySettings.roles.code.role:read": "閲覧",
  "communitySettings.roles.code.role:manage": "管理",
  "communitySettings.roles.code.grant:read": "閲覧",
  "communitySettings.roles.code.grant:manage": "管理",
  "communitySettings.roles.code.group:read": "閲覧",
  "communitySettings.roles.code.group:manage": "管理",
  "communitySettings.roles.code.community:read": "閲覧",
  "communitySettings.roles.code.community:update": "更新",
  "communitySettings.roles.code.community:delete": "削除",
  "communitySettings.roles.code.audit:read": "閲覧",
  // Session permission code label (issue #961).
  "communitySettings.roles.code.session:read": "閲覧",

  // Grants tab (WEBUI_SPEC.md 6.10): per-server permission grants.
  "communitySettings.grants.heading": "権限付与",
  "communitySettings.grants.loading": "権限付与を読み込んでいます…",
  "communitySettings.grants.loadError": "権限付与を読み込めませんでした。",
  "communitySettings.grants.empty": "まだ権限付与がありません。",
  "communitySettings.grants.create": "アクセスを付与…",
  "communitySettings.grants.colMember": "メンバー",
  "communitySettings.grants.colServer": "サーバー",
  "communitySettings.grants.colPermissions": "権限",
  "communitySettings.grants.filterLabel": "メンバーで絞り込み",
  "communitySettings.grants.filterAll": "すべてのメンバー",
  "communitySettings.grants.unknownUser": "（不明なユーザー）",
  "communitySettings.grants.revoke": "取り消し",
  "communitySettings.grants.revoked": "権限付与を取り消しました。",
  "communitySettings.grants.revokeError":
    "権限付与を取り消せませんでした。もう一度お試しください。",
  "communitySettings.grants.revokeDialogTitle": "権限付与を取り消し",
  "communitySettings.grants.revokeDialogBody":
    "このサーバーでのメンバーのサーバーごとの権限を削除します。この操作は元に戻せません。",
  "communitySettings.grants.revokeConfirm": "権限付与を取り消し",
  "communitySettings.grants.createDialogTitle": "サーバーごとのアクセスを付与",
  "communitySettings.grants.createDialogBody":
    "メンバーに、ロールを超えて1つのサーバーで追加の権限を付与します。",
  "communitySettings.grants.memberLabel": "メンバー",
  "communitySettings.grants.memberPlaceholder": "メンバーを選択",
  "communitySettings.grants.serverLabel": "サーバー",
  "communitySettings.grants.serverPlaceholder": "サーバーを選択",
  "communitySettings.grants.permissionsLabel": "権限",
  "communitySettings.grants.createSubmit": "権限付与を作成",
  "communitySettings.grants.created": "権限付与を作成しました。",
  "communitySettings.grants.createIncomplete":
    "メンバー、サーバー、および少なくとも1つの権限を選択してください。",
  "communitySettings.grants.createError":
    "権限付与を作成できませんでした。もう一度お試しください。",

  // General tab.
  "communitySettings.general.heading": "全般",
  "communitySettings.general.nameLabel": "コミュニティ名",
  "communitySettings.general.save": "名前を保存",
  "communitySettings.general.saved": "コミュニティ名を変更しました。",
  "communitySettings.general.nameTaken": "その名前はすでに使用されています。",
  "communitySettings.general.invalidName": "その名前は使用できません。",
  "communitySettings.general.saveError":
    "コミュニティ名を変更できませんでした。もう一度お試しください。",
  "communitySettings.general.dangerHeading": "危険な操作",
  "communitySettings.general.deleteTitle": "コミュニティを削除",
  "communitySettings.general.deleteDesc":
    "このコミュニティのすべてのサーバー、バックアップ、ロール、メンバーシップを削除します。",
  "communitySettings.general.deleteButton": "削除…",
  "communitySettings.general.deleteDialogTitle": "コミュニティを削除",
  "communitySettings.general.deleteDialogBody":
    "コミュニティとその中のすべてを完全に削除します。この操作は元に戻せません。",
  "communitySettings.general.deleteConfirm": "コミュニティを削除",
  "communitySettings.general.deletePrompt":
    "削除を有効にするにはコミュニティ名を入力",
  "communitySettings.general.deleted": "コミュニティを削除しました。",
  "communitySettings.general.deleteError":
    "コミュニティを削除できませんでした。もう一度お試しください。",

  // Community settings — Groups tab (WEBUI_SPEC.md 6.10, issue #464)
  "communitySettings.groups.heading": "プレイヤーグループ",
  "communitySettings.groups.loading": "グループを読み込んでいます…",
  "communitySettings.groups.loadError": "グループを読み込めませんでした。",
  "communitySettings.groups.empty": "まだグループがありません。",
  "communitySettings.groups.create": "新規グループ…",
  "communitySettings.groups.kind.op": "op",
  "communitySettings.groups.kind.whitelist": "whitelist",
  "communitySettings.groups.memberCount": "人",
  "communitySettings.groups.expand": "管理",
  "communitySettings.groups.collapse": "閉じる",
  "communitySettings.groups.rename": "名前を変更…",
  "communitySettings.groups.delete": "削除",
  "communitySettings.groups.error":
    "問題が発生しました。もう一度お試しください。",
  "communitySettings.groups.createDialogTitle": "新規グループ",
  "communitySettings.groups.nameLabel": "グループ名",
  "communitySettings.groups.namePlaceholder": "グループ名",
  "communitySettings.groups.kindLabel": "種類",
  "communitySettings.groups.createSubmit": "グループを作成",
  "communitySettings.groups.nameEmpty": "グループ名を入力してください。",
  "communitySettings.groups.created": "グループを作成しました。",
  "communitySettings.groups.renameDialogTitle": "グループ名を変更",
  "communitySettings.groups.renameSubmit": "名前を保存",
  "communitySettings.groups.renamed": "グループ名を変更しました。",
  "communitySettings.groups.deleteDialogTitle": "グループを削除",
  "communitySettings.groups.deleteDialogBody":
    "このグループを削除すると、適用されているすべてのサーバーから削除されます。この操作は元に戻せません。",
  "communitySettings.groups.deleteConfirm": "グループを削除",
  "communitySettings.groups.deleted": "グループを削除しました。",
  "communitySettings.groups.playersHeading": "プレイヤー",
  "communitySettings.groups.playersEmpty":
    "このグループにはまだプレイヤーがいません。",
  "communitySettings.groups.removePlayer": "削除",
  "communitySettings.groups.removePlayerDialogTitle": "プレイヤーを削除",
  "communitySettings.groups.removePlayerDialogBody":
    "このプレイヤーをグループから削除してもよろしいですか？",
  "communitySettings.groups.removePlayerConfirm": "プレイヤーを削除",
  "communitySettings.groups.playerRemoved": "プレイヤーを削除しました。",
  "communitySettings.groups.addPlayer": "プレイヤーを追加",
  "communitySettings.groups.uuidLabel": "UUID",
  "communitySettings.groups.uuidPlaceholder": "プレイヤーUUID",
  "communitySettings.groups.usernameLabel": "ユーザー名",
  "communitySettings.groups.usernamePlaceholder": "ユーザー名",
  "communitySettings.groups.playerFieldsEmpty":
    "UUIDとユーザー名を入力してください。",
  "communitySettings.groups.playerAdded": "プレイヤーを追加しました。",
  "communitySettings.groups.serversHeading": "適用中のサーバー",
  "communitySettings.groups.serversLoading": "サーバーを読み込んでいます…",
  "communitySettings.groups.serversLoadError":
    "サーバーを読み込めませんでした。",
  "communitySettings.groups.serversEmpty":
    "このグループはまだどのサーバーにも適用されていません。",
  "communitySettings.groups.detach": "解除",
  "communitySettings.groups.detached": "サーバーへの適用を解除しました。",
  "communitySettings.groups.attachHeading": "サーバーに適用",
  "communitySettings.groups.attachEmpty":
    "すべてのコミュニティサーバーに適用済みです。",
  "communitySettings.groups.attach": "適用",
  "communitySettings.groups.attached": "サーバーに適用しました。",
  "communitySettings.groups.unknownServer": "（不明なサーバー）",

  // Platform admin area (WEBUI_SPEC.md 6.12, Section 3) — #474
  "admin.denied.title": "プラットフォーム管理者専用",
  "admin.denied.body": "プラットフォーム管理エリアへのアクセス権がありません。",
  "admin.overview.subtitle": "Workerと全体の統計 — プラットフォーム管理者専用",
  "admin.overview.loading": "プラットフォーム統計を読み込んでいます…",
  "admin.overview.loadError": "プラットフォーム統計を読み込めませんでした。",
  "admin.overview.workers": "Worker",
  "admin.overview.workersOnline": "オンライン",
  "admin.overview.workersDraining": "ドレイン中",
  "admin.overview.workersOffline": "オフライン",
  "admin.overview.servers": "稼働中サーバー",
  "admin.overview.serversHint": "Worker全体で割り当て済み",
  "admin.overview.backups": "バックアップ（全体）",
  "admin.overview.jarPool": "サーバーダウンロード",
  "admin.overview.jars": "JAR",
  "admin.overview.fleet": "Worker一覧",
  "admin.overview.fleetWorker": "Worker",
  "admin.overview.fleetStatus": "ステータス",
  "admin.overview.fleetLoad": "負荷",
  "admin.overview.fleetHeartbeat": "ハートビート",
  "admin.overview.fleetEmpty": "登録されているWorkerがありません。",
  "admin.versions.subtitle":
    "バージョンカタログと共有サーバーダウンロード — プラットフォーム管理者専用",
  "admin.versions.loading": "バージョンカタログを読み込んでいます…",
  "admin.versions.loadError": "バージョンカタログを読み込めませんでした。",
  "admin.versions.catalog": "サーバー種類カタログ",
  "admin.versions.refreshAll": "すべてのカタログを更新",
  "admin.versions.refresh": "更新",
  "admin.versions.refreshing": "更新しています…",
  "admin.versions.type": "サーバーの種類",
  "admin.versions.count": "バージョン数",
  "admin.versions.latest": "最新",
  "admin.versions.empty": "カタログ化されたサーバーの種類がありません。",
  "admin.versions.typeError": "利用不可",
  "admin.versions.refreshedAll": "バージョン一覧を再読み込みします。",
  // Interpolated with the server type, e.g. "Refreshed catalog: paper".
  "admin.versions.refreshedOne": "カタログを更新しました: {type}",
  "admin.versions.refreshError": "カタログを更新できませんでした。",
  "admin.versions.jarPool": "サーバーダウンロード",
  "admin.versions.jarPoolCached": "ダウンロード済みJAR",
  "admin.versions.jarPoolSize": "合計サイズ",
  "admin.versions.gc": "ディスク容量を解放",
  "admin.versions.gcRunning": "実行しています…",
  "admin.versions.gcHint":
    "どのサーバーも使用していないダウンロード済みサーバーJARを削除します。",
  "admin.versions.gcDialog.title": "ディスク容量を解放しますか?",
  "admin.versions.gcDialog.body":
    "稼働中のどのサーバーも使用していないダウンロード済みサーバーJARを削除します。次に必要になったときに再ダウンロードされます。",
  "admin.versions.gcDialog.confirm": "未使用ファイルを削除",
  // Interpolated with freed bytes + deleted count, e.g.
  // "Freed 412 MiB by deleting 3 unused JARs.".
  "admin.versions.gcDone":
    "未使用のJAR {count} 個を削除し、{bytes} を解放しました。",
  "admin.versions.gcError": "ディスク容量を解放できませんでした。",
  // Communities (WEBUI_SPEC.md 6.12) — #476, #489
  "admin.communities.subtitle":
    "プラットフォーム上のすべてのコミュニティ。プロビジョニングは管理者専用で、セルフサービスでの作成には対応していません。",
  "admin.communities.loading": "コミュニティを読み込んでいます…",
  "admin.communities.loadError": "コミュニティを読み込めませんでした。",
  "admin.communities.empty": "まだコミュニティがありません。",
  "admin.communities.colName": "名前",
  "admin.communities.colId": "ID",
  "admin.communities.colMembers": "メンバー数",
  "admin.communities.colServers": "サーバー数",
  "admin.communities.colActions": "操作",
  "admin.communities.delete": "削除",
  "admin.communities.deleteTitle": "コミュニティを削除",
  "admin.communities.deleteBody":
    "コミュニティとその中のすべて（メンバー、ロール、サーバー）を完全に削除します。この操作は元に戻せません。",
  "admin.communities.deletePrompt":
    "確認のためコミュニティ名を入力してください:",
  "admin.communities.deleteConfirm": "コミュニティを削除",
  "admin.communities.deleted": "コミュニティを削除しました。",
  "admin.communities.deleteError": "コミュニティを削除できませんでした。",
  "admin.communities.prev": "前へ",
  "admin.communities.next": "次へ",
  "admin.communities.range": "{total} 件中 {from}–{to}",
  "admin.communities.provision": "コミュニティをプロビジョニング",
  "admin.communities.provisionSubmit": "プロビジョニング",
  "admin.communities.dialogTitle": "コミュニティをプロビジョニング",
  "admin.communities.nameLabel": "コミュニティ名",
  "admin.communities.namePlaceholder": "例: Winter Server 2026",
  "admin.communities.ownerLabel": "初期オーナー",
  "admin.communities.ownerPlaceholder": "既存のアカウントを選択…",
  "admin.communities.ownerHint":
    "オーナーにはプリセットのOwnerロール（すべてのコミュニティ権限）が付与されます。",
  "admin.communities.usersLoadError": "ユーザー一覧を読み込めませんでした。",
  // Truncation hint interpolated with the loaded/total counts, e.g.
  // "Showing the first 100 of 150 users."
  "admin.communities.usersTruncated":
    "{total} 人中、最初の {n} 人のユーザーを表示しています。",
  "admin.communities.provisioned": "コミュニティをプロビジョニングしました。",
  "admin.communities.errNameRequired": "コミュニティ名を入力してください。",
  "admin.communities.errOwnerRequired": "初期オーナーを選択してください。",
  "admin.communities.errNameTaken":
    "その名前のコミュニティはすでに存在します。",
  "admin.communities.errInvalidName": "そのコミュニティ名は無効です。",
  "admin.communities.errOwnerNotFound":
    "そのオーナーアカウントはもう存在しません。",
  "admin.communities.errGeneric":
    "コミュニティをプロビジョニングできませんでした。",

  // Workers fleet page (WEBUI_SPEC.md 6.12) — #477
  "admin.workers.subtitle":
    "Workerはコントロールプレーン経由で自己登録します。メンテナンス前にサーバーを移すにはドレインしてください。",
  "admin.workers.loading": "Workerを読み込んでいます…",
  "admin.workers.loadError": "Workerを読み込めませんでした。",
  "admin.workers.empty": "登録されているWorkerがありません。",
  "admin.workers.colWorker": "Worker",
  "admin.workers.colStatus": "ステータス",
  "admin.workers.colVersion": "バージョン",
  "admin.workers.colDrivers": "ドライバ",
  "admin.workers.colLoad": "負荷",
  "admin.workers.colResources": "リソース",
  "admin.workers.colHeartbeat": "ハートビート",
  "admin.workers.cpuCores": "c",
  "admin.workers.drain": "ドレイン",
  "admin.workers.undrain": "ドレイン解除",
  "admin.workers.drainDialogTitle": "Workerをドレイン",
  "admin.workers.drainDialogBody":
    "ドレインすると、このWorkerへの新規配置を停止し、稼働中のサーバーを最終スナップショットとともに停止して、別の場所で再起動できるようにします。",
  "admin.workers.drainConfirm": "Workerをドレイン",
  "admin.workers.undrainDialogTitle": "Workerのドレインを解除",
  "admin.workers.undrainDialogBody":
    "ドレインを解除すると、このWorkerは再び新規配置を受け入れられるようになります。",
  "admin.workers.undrainConfirm": "Workerのドレインを解除",
  "admin.workers.drained": "Workerをドレインしました。",
  // Shown when servers_stopped > 0, interpolated with the count.
  "admin.workers.drainedCount":
    "Workerをドレインしました。{count} 台のサーバーを停止対象としてマークしました — 各サーバーが停止済み・未割り当てになるまでこのWorkerを接続したままにしてください。",
  "admin.workers.drainDialogConvergenceWarning":
    "停止と最終スナップショットは、Workerが接続している間のみ実行されます。このWorkerに割り当てられていたすべてのサーバーが停止済み・未割り当てになるまで、このWorkerを起動したままにしてください。早期に停止すると、それらのサーバーは最終スナップショットを取れなくなります。各サーバーが停止したかどうかはサーバー一覧で確認してください。",
  "admin.workers.undrained": "Workerのドレインを解除しました。",
  "admin.workers.drainError": "Workerをドレインできませんでした。",
  "admin.workers.undrainError": "Workerのドレインを解除できませんでした。",
  "admin.workers.notice":
    "ドレインすると、Workerへの新規配置を停止します。稼働中のサーバーは最終スナップショットとともに停止され、別の場所で再起動できます。オフラインのWorkerは再接続時に自動的に再表示されます。",

  // Admin Users page (WEBUI_SPEC.md 6.12) — #475
  "admin.users.loading": "ユーザーを読み込んでいます…",
  "admin.users.loadError": "ユーザーを読み込めませんでした。",
  "admin.users.empty": "ユーザーがいません。",
  "admin.users.count": "アカウント",
  "admin.users.colUsername": "ユーザー名",
  "admin.users.colEmail": "メールアドレス",
  "admin.users.colStatus": "ステータス",
  "admin.users.colAdmin": "管理者",
  "admin.users.colCreated": "作成日時",
  "admin.users.you": "あなた",
  "admin.users.statusActive": "有効",
  "admin.users.statusDeactivated": "無効",
  "admin.users.adminYes": "管理者",
  "admin.users.adminNo": "—",
  "admin.users.prev": "‹ 前へ",
  "admin.users.next": "次へ ›",
  "admin.users.range": "{from}–{to} / {total}",
  "admin.users.deactivate": "無効化",
  "admin.users.reactivate": "再有効化",
  "admin.users.makeAdmin": "管理者にする",
  "admin.users.revokeAdmin": "管理者を解除",
  "admin.users.delete": "削除",
  "admin.users.deactivated": "ユーザーを無効化しました。",
  "admin.users.reactivated": "ユーザーを再有効化しました。",
  "admin.users.adminGranted": "プラットフォーム管理者を付与しました。",
  "admin.users.adminRevoked": "プラットフォーム管理者を解除しました。",
  "admin.users.deleted": "ユーザーを削除しました。",
  "admin.users.selfRevokeTitle": "自分自身の管理者権限を解除しますか?",
  "admin.users.selfRevokeBody":
    "自分自身のプラットフォーム管理者アクセスを解除しようとしています。直ちに管理エリアへのアクセスを失います。",
  "admin.users.selfRevokeConfirm": "自分の管理者権限を解除",
  "admin.users.deleteTitle": "ユーザーを削除",
  "admin.users.deleteBody":
    "アカウントを完全に削除します。確認のためユーザー名を入力してください。",
  "admin.users.deletePrompt": "ユーザー名",
  "admin.users.deleteConfirm": "ユーザーを削除",
  // Conflict reasons the lifecycle routes return (admin_users.py).
  "admin.users.error.self_target":
    "この画面では自分自身のアカウントを操作できません。アカウントページから行ってください。",
  "admin.users.error.last_platform_admin":
    "最後の有効なプラットフォーム管理者は削除できません。",
  "admin.users.error.owns_community":
    "このユーザーはコミュニティを所有しているため削除できません。",
  "admin.users.error.not_found": "そのユーザーはもう存在しません。",
  "admin.users.error.generic": "操作を完了できませんでした。",
  // Create-user dialog (POST /admin/users).
  "admin.users.create": "ユーザーを作成",
  "admin.users.createTitle": "ユーザーを作成",
  "admin.users.createSubmit": "作成",
  "admin.users.createSubmitting": "作成しています…",
  "admin.users.created": "ユーザーを作成しました。",
  "admin.users.usernameLabel": "ユーザー名",
  "admin.users.emailLabel": "メールアドレス",
  "admin.users.passwordLabel": "パスワード",
  "admin.users.passwordHint":
    "12文字以上、大文字小文字・数字・記号を混在させてください。",

  // Admin global Audit page (WEBUI_SPEC.md 6.12).
  "admin.audit.filterCommunity": "コミュニティ",
  "admin.audit.filterCommunityAll": "すべてのコミュニティ",
  "admin.audit.colCommunity": "コミュニティ",
  "admin.audit.communitiesTruncated":
    "{total} 件中、最初の {n} 件のコミュニティを表示しています。",

  // Plugins tab (issue #1153).
  "serverDetail.tab.plugins": "プラグイン",
  // Loader-aware tab label + noun (#1320): Fabric/Forge は Mod、Paper はプラグイン
  // を管理する。日本語では語形は一つだが、英語の `{nouns}` / `{noun}` / `{Noun}`
  // と対になるよう三つのキーを揃える。
  "serverDetail.tab.mods": "Mod",
  "plugins.contentNoun.plural.plugins": "プラグイン",
  "plugins.contentNoun.plural.mods": "Mod",
  "plugins.contentNoun.singular.plugins": "プラグイン",
  "plugins.contentNoun.singular.mods": "Mod",
  "plugins.contentNoun.singularCap.plugins": "プラグイン",
  "plugins.contentNoun.singularCap.mods": "Mod",
  "plugins.loading": "{nouns}を読み込み中…",
  "plugins.loadError": "{nouns}を読み込めませんでした。",
  "plugins.noRead": "{nouns}を閲覧する権限がありません。",
  "plugins.empty": "{nouns}がインストールされていません。",
  "plugins.unsupported":
    "このサーバータイプはプラグインやModに対応していません。",
  "plugins.serverNotStopped":
    "{nouns}を管理するにはサーバーを停止してください。",
  "plugins.col.name": "名前",
  "plugins.col.version": "バージョン",
  "plugins.col.source": "ソース",
  "plugins.col.side": "動作環境",
  "plugins.col.status": "状態",
  "plugins.col.size": "サイズ",
  "plugins.col.actions": "操作",
  "plugins.status.enabled": "有効",
  "plugins.status.disabled": "無効",
  "plugins.source.local": "ローカル",
  "plugins.source.modrinth": "Modrinth",
  "plugins.side.label": "動作環境",
  "plugins.side.server": "サーバー",
  "plugins.side.client": "クライアント",
  "plugins.side.both": "両方",
  "plugins.enable": "有効化",
  "plugins.disable": "無効化",
  "plugins.remove": "削除",
  "plugins.update": "更新",
  "plugins.install": "JARをアップロード",
  "plugins.browse": "Modrinthで検索",
  "plugins.downloadClientModpack": "クライアント用ModをDL",
  "plugins.updateAvailable": "更新あり: ",
  "plugins.removeDialog.title": "{name}を削除しますか？",
  "plugins.removeDialog.body": "この{noun}をサーバーから完全に削除します。",
  "plugins.removeDialog.confirm": "削除",
  "plugins.dependencies": "依存関係",
  "plugins.dependencies.loading": "依存関係を読み込み中…",
  "plugins.dependencies.empty": "依存関係なし。",
  "plugins.dependencies.required": "必須",
  "plugins.dependencies.optional": "オプション",
  "plugins.dependencies.installed": "インストール済み",
  "plugins.dependencies.missing": "未インストール",
  "plugins.search.title": "Modrinthで検索",
  "plugins.search.placeholder": "プラグインやModを検索…",
  "plugins.search.empty": "結果がありません。",
  "plugins.search.downloads": "ダウンロード",
  "plugins.search.by": "作者:",
  "plugins.search.install": "インストール",
  "plugins.search.installing": "インストール中…",
  "plugins.search.installed": "インストール済み",
  "plugins.search.update": "更新",
  "plugins.search.versions": "バージョン",
  "plugins.search.back": "検索に戻る",
  "plugins.enabled": "{Noun}を有効にしました。",
  "plugins.disabled": "{Noun}を無効にしました。",
  "plugins.removed": "{Noun}を削除しました。",
  "plugins.updated": "{Noun}を更新しました。",
  "plugins.installed": "{Noun}をインストールしました。",
  "plugins.sideUpdated": "{Noun}のサイドを更新しました。",
  "plugins.catalogInstalled": "Modrinthから{Noun}をインストールしました。",
  "plugins.error.alreadyExists":
    "同じ名前またはプロジェクトの{noun}がすでにインストールされています。",
  "plugins.error.notStopped":
    "{nouns}を管理するにはサーバーを停止してください。",
  "plugins.error.unsettled":
    "サーバーの準備ができていません。現在の操作が完了するまでお待ちください。",
  "plugins.error.busy": "別の操作が進行中です。しばらくお待ちください。",
  "plugins.error.invalidPath":
    "無効なファイルです。{nouns}としてアップロードできるのは .jar ファイルのみです。",
  "plugins.error.tooLarge":
    "ファイルが大きすぎます。アップロードの上限は 512 MB です。",
  "plugins.error.catalogUnavailable":
    "Modrinth に接続できませんでした。しばらくしてからもう一度お試しください。",
  "plugins.error.catalogNotFound":
    "Modrinth でプロジェクトまたはバージョンが見つかりませんでした。",
  "plugins.error.checksumMismatch":
    "ダウンロードの整合性チェックに失敗しました。もう一度お試しください。",
  "plugins.error.unsupportedServerType":
    "このサーバータイプは{nouns}に対応していません。",
  "plugins.error.invalidSide": "このサーバータイプには無効なサイドです。",
  "plugins.error.notFound":
    "{Noun}が見つかりません。削除された可能性があります。",
  "plugins.error.generic": "エラーが発生しました。もう一度お試しください。",
  // Dependency / compatibility validation checklist (issue #1307).
  "plugins.validation.heading": "依存関係と互換性",
  "plugins.validation.ok": "問題は見つかりませんでした。",
  "plugins.validation.missingDep":
    "{mod} は {dependency}（{range}）を必要としますが、インストールされていません。",
  "plugins.validation.missingCatalogDep":
    "{mod} は {dependency} を必要としますが、インストールされていません。",
  "plugins.validation.versionUnsatisfied":
    "{mod} は {dependency}（{range}）を必要としますが、インストール済みの {present} は条件を満たしません。",
  "plugins.validation.conflict": "{mod} は {other} と競合します。",
  "plugins.validation.mcMismatch":
    "{mod} は MC {serverVersion} に対応していません（対応: {modVersions}）。",
  // 依存関係の自動解決 (issue #1309)。
  "plugins.resolve.action": "依存関係を解決",
  "plugins.resolve.loading": "解決プランを計算中…",
  "plugins.resolve.title": "依存関係を解決",
  "plugins.resolve.nothing": "必要な依存関係はすべて満たされています。",
  "plugins.resolve.importsHeading": "Modrinth からインストールします",
  "plugins.resolve.importItem": "{dependency} → {project} {version}",
  "plugins.resolve.satisfiedHeading": "解決済み",
  "plugins.resolve.satisfiedItem": "{dependency}",
  "plugins.resolve.conflictsHeading": "競合によりブロック",
  "plugins.resolve.conflictItem":
    "{dependency} はインストールできません（インストール済みプラグインと競合します）。",
  "plugins.resolve.unresolvableHeading": "解決できません",
  "plugins.resolve.unresolvableItem":
    "{dependency} — 互換性のある Modrinth バージョンが見つかりません。",
  "plugins.resolve.apply": "依存関係をインストール",
  "plugins.resolve.cancel": "キャンセル",
  "plugins.resolve.applied": "依存関係をインストールしました。",
  "plugins.resolve.appliedWithFailures":
    "一部の依存関係をインストールできませんでした。",
  "communitySettings.roles.family.plugin": "プラグイン",
  "communitySettings.roles.code.plugin:read": "閲覧",
  "communitySettings.roles.code.plugin:manage": "管理",
  "communitySettings.audit.op.plugin:install": "プラグインをインストール",
  "communitySettings.audit.op.plugin:remove": "プラグインを削除",
  "communitySettings.audit.op.plugin:enable": "プラグインを有効化",
  "communitySettings.audit.op.plugin:disable": "プラグインを無効化",
  "communitySettings.audit.op.plugin:update": "プラグインを更新",
  "communitySettings.audit.op.plugin:set_side": "プラグインのサイドを設定",
  "communitySettings.audit.op.plugin:resolve": "プラグインの依存関係を解決",
  "communitySettings.audit.targetType.plugin": "プラグイン",

  // Resource pack library (issue #1178).
  "nav.resourcePacks": "リソースパック",
  "page.resourcePacks": "リソースパック",
  "resourcePacks.subtitle":
    "Minecraftサーバーで使用するリソースパックのアップロードと管理。",
  "resourcePacks.loading": "リソースパックを読み込んでいます…",
  "resourcePacks.loadError": "リソースパックを読み込めませんでした。",
  "resourcePacks.empty": "まだリソースパックがありません。",
  "resourcePacks.upload": "パックをアップロード",
  "resourcePacks.col.displayName": "名前",
  "resourcePacks.col.filename": "ファイル名",
  "resourcePacks.col.size": "サイズ",
  "resourcePacks.col.sha1": "SHA-1",
  "resourcePacks.col.uploaded": "アップロード日時",
  "resourcePacks.col.uploader": "アップロード者",
  "resourcePacks.download": "ダウンロード",
  "resourcePacks.delete": "削除",
  "resourcePacks.uploadDialog.title": "リソースパックをアップロード",
  "resourcePacks.uploadDialog.displayName": "表示名",
  "resourcePacks.uploadDialog.file": "ファイル（.zip）",
  "resourcePacks.uploadDialog.submit": "アップロード",
  "resourcePacks.uploadDialog.uploading": "アップロードしています…",
  "resourcePacks.uploaded": "リソースパックをアップロードしました。",
  "resourcePacks.deleted": "リソースパックを削除しました。",
  "resourcePacks.deleteDialog.title": "リソースパックを削除",
  "resourcePacks.deleteDialog.body":
    "リソースパックを完全に削除します。サーバーに割り当て済みのパックは削除できません。",
  "resourcePacks.deleteDialog.confirm": "パックを削除",
  "resourcePacks.deleteDialog.prompt": "削除を有効にするには表示名を入力",
  "resourcePacks.error.tooLarge":
    "ファイルサイズが256 MiBのアップロード上限を超えています。",
  "resourcePacks.error.uploadFailed":
    "リソースパックをアップロードできませんでした。",
  "resourcePacks.error.deleteFailed": "リソースパックを削除できませんでした。",
  "resourcePacks.error.inUse":
    "このリソースパックは1つ以上のサーバーに割り当てられているため、削除できません。",
  "resourcePacks.error.downloadFailed":
    "リソースパックをダウンロードできませんでした。",

  // Server resource pack assignment (issue #1179).
  "serverDetail.resourcePack.heading": "リソースパック",
  "serverDetail.resourcePack.none": "リソースパックが割り当てられていません。",
  "serverDetail.resourcePack.assign": "割り当て",
  "serverDetail.resourcePack.change": "変更",
  "serverDetail.resourcePack.remove": "解除",
  "serverDetail.resourcePack.name": "名前",
  "serverDetail.resourcePack.filename": "ファイル名",
  "serverDetail.resourcePack.size": "サイズ",
  "serverDetail.resourcePack.sha1": "SHA-1",
  "serverDetail.resourcePack.url": "公開URL",
  "serverDetail.resourcePack.urlCopied": "コピーしました！",
  "serverDetail.resourcePack.required": "必須",
  "serverDetail.resourcePack.notRequired": "任意",
  "serverDetail.resourcePack.prompt": "プロンプト",
  "serverDetail.resourcePack.promptNone": "なし",
  "serverDetail.resourcePack.notAtRest":
    "リソースパックの設定を変更するにはサーバーを停止してください。",
  "serverDetail.resourcePack.assigned": "リソースパックを割り当てました。",
  "serverDetail.resourcePack.unassigned":
    "リソースパックの割り当てを解除しました。",
  "serverDetail.resourcePack.assignError":
    "リソースパックを割り当てできませんでした。",
  "serverDetail.resourcePack.unassignError":
    "リソースパックの割り当てを解除できませんでした。",
  "serverDetail.resourcePack.assignDialog.title": "リソースパックを割り当て",
  "serverDetail.resourcePack.assignDialog.select": "リソースパック",
  "serverDetail.resourcePack.assignDialog.selectPlaceholder": "パックを選択…",
  "serverDetail.resourcePack.assignDialog.require":
    "リソースパックを必須にする",
  "serverDetail.resourcePack.assignDialog.prompt":
    "カスタムプロンプト（プレイヤーに表示）",
  "serverDetail.resourcePack.assignDialog.submit": "割り当て",
  "serverDetail.resourcePack.assignDialog.loading": "パックを読み込んでいます…",
  "serverDetail.resourcePack.assignDialog.empty":
    "利用可能なパックがありません。",
  "serverDetail.resourcePack.removeDialog.title": "リソースパックを解除",
  "serverDetail.resourcePack.removeDialog.body":
    "このサーバーからリソースパックの割り当てを解除しますか？",
  "serverDetail.resourcePack.removeDialog.confirm": "解除",

  // Permission / authorization feedback (WEBUI_SPEC.md 7.3 / 7.4)
  "permissions.denied": "この操作を行う権限がありません。",
  // Interpolated with the missing permission code, e.g. "You lack: server:start".
  "permissions.deniedNamed": "不足している権限: {permission}",

  // Error boundary (#1211)
  "errorBoundary.title": "問題が発生しました",
  "errorBoundary.body":
    "予期しないエラーが発生しました。ページを再読み込みすると復旧することがあります。",
  "errorBoundary.reload": "ページを再読み込み",

  // Shared format strings — heartbeat age (#1214)
  "format.secondsAgo": "{value}秒前",
  "format.minutesAgo": "{value}分前",
  "format.hoursAgo": "{value}時間前",
} as const;
