/* Hermes Quality Loop — web dashboard plugin (plain IIFE, no build step). */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !SDK.React) return;

  const React = SDK.React;
  const h = React.createElement;
  const components = SDK.components || {};
  const Card = components.Card || "section";
  const CardHeader = components.CardHeader || "header";
  const CardTitle = components.CardTitle || "h2";
  const CardContent = components.CardContent || "div";
  const Button = components.Button || "button";
  const Input = components.Input || "input";
  const Label = components.Label || "label";
  const Badge = components.Badge || "span";
  const useState = React.useState;
  const useEffect = React.useEffect;
  const useCallback = React.useCallback;

  const API = "/api/plugins/quality-loop";
  const DEFAULTS = {
    name: "Codebase Quality Loop",
    board: "default",
    workspace: "",
    assignee: "",
    examiner_model: "",
    executor_model: "",
    validator_model: "",
    provider_override: "",
    build_command: "",
    test_command: "",
    gate_timeout_seconds: "1800",
    target_average: "",
    publish_on_success: false,
    publish_remote: "origin",
    publish_branch: "",
    commit_message: "quality-loop: reach target quality average",
    max_rounds: "3",
    max_repairs: "1"
  };

  function apiError(err) {
    const raw = err && err.message ? String(err.message) : String(err || "Unknown error");
    const match = raw.match(/^(\d{3}):\s*(.*)$/s);
    const body = match ? match[2] : raw;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
    } catch (_ignored) {}
    return body || raw;
  }

  function Field(props) {
    return h("div", { className: props.wide ? "space-y-1.5 md:col-span-2" : "space-y-1.5" },
      h(Label, { htmlFor: props.name, className: "text-sm font-medium" }, props.label),
      h(Input, {
        id: props.name,
        name: props.name,
        type: props.type || "text",
        value: props.type === "checkbox" ? undefined : props.value,
        checked: props.type === "checkbox" ? !!props.value : undefined,
        min: props.min,
        max: props.max,
        required: !!props.required,
        placeholder: props.placeholder || "",
        onChange: function (event) {
          props.onChange(props.name, props.type === "checkbox" ? event.target.checked : event.target.value);
        }
      }),
      props.help ? h("p", { className: "text-xs text-muted-foreground" }, props.help) : null
    );
  }

  function statusVariant(state) {
    if (state === "succeeded") return "default";
    if (state === "paused" || state === "stopped") return "secondary";
    if (state === "failed") return "destructive";
    return "outline";
  }

  function CampaignCard(props) {
    const campaign = props.campaign;
    const active = campaign.active_task;
    const state = campaign.state || "unknown";
    const actions = [];
    if (state === "running") actions.push("pause");
    if (state === "paused" || state === "stopped" || state === "failed") actions.push("resume");
    if (state !== "succeeded" && state !== "stopped") actions.push("stop");
    actions.push("reconcile");

    return h(Card, { className: "border-border/70" },
      h(CardHeader, { className: "pb-3" },
        h("div", { className: "flex flex-wrap items-center justify-between gap-2" },
          h(CardTitle, { className: "text-base" }, campaign.name || campaign.id),
          h(Badge, { variant: statusVariant(state) }, state)
        )
      ),
      h(CardContent, { className: "space-y-3 text-sm" },
        h("dl", { className: "grid grid-cols-[auto_1fr] gap-x-3 gap-y-1" },
          h("dt", { className: "text-muted-foreground" }, "Stage"),
          h("dd", null, (campaign.stage || "—") + " · round " + (campaign.round_no || 0) + "/" + (campaign.max_rounds || 0)),
          h("dt", { className: "text-muted-foreground" }, "Workspace"),
          h("dd", { className: "break-all font-mono text-xs" }, campaign.workspace || "—"),
          h("dt", { className: "text-muted-foreground" }, "Board / assignee"),
          h("dd", null, (campaign.board || "—") + " / " + (campaign.assignee || "—")),
          campaign.target_average != null ? h("dt", { className: "text-muted-foreground" }, "Ranking") : null,
          campaign.target_average != null ? h("dd", null, (campaign.last_average == null ? "not scored" : campaign.last_average + "/10") + " · target " + campaign.target_average + "/10") : null,
          active ? h("dt", { className: "text-muted-foreground" }, "Active card") : null,
          active ? h("dd", null, active.title + " (" + active.status + ")") : null
        ),
        campaign.message ? h("p", { className: "rounded-md border border-border/60 bg-muted/30 p-2 text-muted-foreground" }, campaign.message) : null,
        h("div", { className: "flex flex-wrap gap-2" },
          actions.map(function (action) {
            return h(Button, {
              key: action,
              type: "button",
              size: "sm",
              variant: action === "stop" ? "destructive" : "outline",
              disabled: props.busy,
              onClick: function () { props.onAction(campaign.id, action); }
            }, action.charAt(0).toUpperCase() + action.slice(1));
          })
        )
      )
    );
  }

  function QualityLoopPage() {
    const [form, setForm] = useState(function () { return Object.assign({}, DEFAULTS); });
    const [campaigns, setCampaigns] = useState([]);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [notice, setNotice] = useState("");

    const loadCampaigns = useCallback(function () {
      setLoading(true);
      return SDK.fetchJSON(API + "/campaigns")
        .then(function (data) {
          setCampaigns((data && data.campaigns) || []);
          setError("");
        })
        .catch(function (err) { setError(apiError(err)); })
        .finally(function () { setLoading(false); });
    }, []);

    useEffect(function () {
      loadCampaigns();
      const timer = window.setInterval(loadCampaigns, 5000);
      return function () { window.clearInterval(timer); };
    }, [loadCampaigns]);

    function update(name, value) {
      setForm(function (current) {
        const next = Object.assign({}, current);
        next[name] = value;
        return next;
      });
      setNotice("");
    }

    function startCampaign(event) {
      event.preventDefault();
      setError("");
      setNotice("");
      if (!form.workspace.trim()) {
        setError("Workspace is required and must be an existing absolute directory.");
        return;
      }
      if (!form.build_command.trim() && !form.test_command.trim()) {
        setError("Enter at least one fixed build or test command.");
        return;
      }
      const payload = Object.assign({}, form, {
        gate_timeout_seconds: Number(form.gate_timeout_seconds),
        target_average: form.target_average === "" ? null : Number(form.target_average),
        publish_on_success: !!form.publish_on_success,
        publish_branch: form.publish_branch.trim() || null,
        provider_override: form.provider_override.trim() || null,
        max_rounds: Number(form.max_rounds),
        max_repairs: Number(form.max_repairs)
      });
      setBusy(true);
      SDK.fetchJSON(API + "/campaigns", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }).then(function (data) {
        const created = data && data.campaign;
        setNotice("Campaign " + (created ? created.id : "") + " started.");
        if (created) {
          setCampaigns(function (items) { return [created].concat(items.filter(function (item) { return item.id !== created.id; })); });
        } else {
          loadCampaigns();
        }
      }).catch(function (err) {
        setError(apiError(err));
      }).finally(function () {
        setBusy(false);
      });
    }

    function act(campaignId, action) {
      setBusy(true);
      setError("");
      setNotice("");
      SDK.fetchJSON(API + "/campaigns/" + encodeURIComponent(campaignId) + "/" + action, {
        method: "POST"
      }).then(function (data) {
        const updated = data && data.campaign;
        if (updated) {
          setCampaigns(function (items) {
            return items.map(function (item) { return item.id === updated.id ? updated : item; });
          });
        }
        setNotice("Campaign " + campaignId + ": " + action + " completed.");
      }).catch(function (err) {
        setError(apiError(err));
      }).finally(function () {
        setBusy(false);
      });
    }

    const canStart = !!form.workspace.trim() && !!(form.build_command.trim() || form.test_command.trim()) && !busy;

    return h("div", { className: "space-y-6" },
      h("div", null,
        h("h1", { className: "text-2xl font-semibold tracking-tight" }, "Quality Loop"),
        h("p", { className: "mt-1 text-sm text-muted-foreground" }, "Create a bounded examine → execute → validate campaign backed by durable Kanban cards and deterministic gates.")
      ),
      error ? h("div", { className: "rounded-md border border-destructive/60 bg-destructive/10 px-4 py-3 text-sm text-destructive" }, error) : null,
      notice ? h("div", { className: "rounded-md border border-border bg-muted/40 px-4 py-3 text-sm" }, notice) : null,
      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-lg" }, "Start a campaign")),
        h(CardContent, null,
          h("form", { className: "space-y-5", onSubmit: startCampaign },
            h("div", { className: "grid gap-4 md:grid-cols-2" },
              h(Field, { name: "name", label: "Campaign name", value: form.name, onChange: update, required: true }),
              h(Field, { name: "workspace", label: "Workspace", value: form.workspace, onChange: update, required: true, placeholder: "/absolute/path/to/clean/worktree", help: "Must already exist. Use a dedicated clean worktree." }),
              h(Field, { name: "board", label: "Kanban board", value: form.board, onChange: update, required: true }),
              h(Field, { name: "assignee", label: "Assignee profile", value: form.assignee, onChange: update, required: true }),
              h(Field, { name: "examiner_model", label: "Examiner model", value: form.examiner_model, onChange: update, required: true }),
              h(Field, { name: "executor_model", label: "Executor model", value: form.executor_model, onChange: update, required: true }),
              h(Field, { name: "validator_model", label: "Validator model", value: form.validator_model, onChange: update, required: true }),
              h(Field, { name: "provider_override", label: "Provider override", value: form.provider_override, onChange: update, placeholder: "Optional, e.g. openai-codex" }),
              h(Field, { name: "target_average", label: "Target average (0–10)", value: form.target_average, onChange: update, type: "number", min: 0.1, max: 10, placeholder: "Optional; e.g. 9" }),
              h(Field, { name: "publish_on_success", label: "Commit and push after final PASS", value: form.publish_on_success, onChange: update, type: "checkbox", help: "Requires a ranking target and a dedicated Git worktree." }),
              h(Field, { name: "publish_remote", label: "Publish remote", value: form.publish_remote, onChange: update }),
              h(Field, { name: "publish_branch", label: "Publish branch", value: form.publish_branch, onChange: update, placeholder: "Current branch when blank" }),
              h(Field, { name: "commit_message", label: "Commit message", value: form.commit_message, onChange: update }),
              h("div", { className: "grid grid-cols-3 gap-3" },
                h(Field, { name: "max_rounds", label: "Rounds", value: form.max_rounds, onChange: update, type: "number", min: 1, max: 100, required: true }),
                h(Field, { name: "max_repairs", label: "Repairs", value: form.max_repairs, onChange: update, type: "number", min: 0, max: 20, required: true }),
                h(Field, { name: "gate_timeout_seconds", label: "Timeout (s)", value: form.gate_timeout_seconds, onChange: update, type: "number", min: 10, max: 3600, required: true })
              ),
              h(Field, { name: "build_command", label: "Build command", value: form.build_command, onChange: update, wide: true, placeholder: "Optional when a test command is supplied" }),
              h(Field, { name: "test_command", label: "Test command", value: form.test_command, onChange: update, wide: true, placeholder: "Required unless a build command is supplied", help: "Commands run non-interactively in the workspace after validation." })
            ),
            h("div", { className: "flex items-center gap-3" },
              h(Button, { type: "submit", disabled: !canStart }, busy ? "Starting…" : "Start campaign"),
              !canStart && !busy ? h("span", { className: "text-xs text-muted-foreground" }, "Enter a workspace and at least one build or test command.") : null
            )
          )
        )
      ),
      h("div", { className: "flex items-center justify-between" },
        h("h2", { className: "text-lg font-semibold" }, "Campaigns"),
        h(Button, { type: "button", variant: "outline", size: "sm", onClick: loadCampaigns, disabled: loading || busy }, loading ? "Refreshing…" : "Refresh")
      ),
      loading && campaigns.length === 0 ? h("p", { className: "text-sm text-muted-foreground" }, "Loading campaigns…") : null,
      !loading && campaigns.length === 0 ? h("p", { className: "rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground" }, "No campaigns yet.") : null,
      h("div", { className: "grid gap-4 lg:grid-cols-2" },
        campaigns.map(function (campaign) {
          return h(CampaignCard, { key: campaign.id, campaign: campaign, onAction: act, busy: busy });
        })
      )
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("quality-loop", QualityLoopPage);
  }
})();
