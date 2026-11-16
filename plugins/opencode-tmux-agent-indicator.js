// tmux-agent-indicator plugin for OpenCode.
// Install to ~/.config/opencode/plugins/ or .opencode/plugins/ (project-level).
// Tracks session state and calls agent-state.sh to update tmux pane visuals.

export const TmuxAgentIndicator = async ({ $ }) => {
  const dir = process.env.TMUX_AGENT_INDICATOR_DIR
    || `${process.env.HOME}/.tmux/plugins/tmux-agent-indicator`;
  const script = `${dir}/scripts/agent-state.sh`;

  let lastState = "off";
  let idleAt = 0;
  let updates = Promise.resolve();

  const setState = (state) => {
    // OpenCode must not wait for tmux's state lock. Serialize updates here so
    // a slow running update cannot overtake a later permission or completion.
    updates = updates.then(async () => {
      if (state === lastState) return;
      try {
        await $`bash ${script} --agent opencode --state ${state}`;
        lastState = state;
      } catch {
        // A failed update must not break the queue or suppress the next retry.
        lastState = null;
      }
    });
  };

  return {
    event: async ({ event }) => {
      if (event.type === "session.status"
          && event.properties.status.type === "busy") {
        // Guard: don't override done/error if idle fired recently (race condition)
        if (Date.now() - idleAt < 2000) return;
        setState("running");
      }

      if (event.type === "permission.updated"
          || event.type === "permission.asked") {
        setState("needs-input");
      }

      if (event.type === "session.idle") {
        idleAt = Date.now();
        setState("done");
      }

      if (event.type === "session.error") {
        idleAt = Date.now();
        setState("done");
      }
    },
    "permission.ask": async () => {
      setState("needs-input");
    },
    "tool.execute.before": async (input) => {
      if (input.tool === "question") {
        setState("needs-input");
      }
    },
  };
};
