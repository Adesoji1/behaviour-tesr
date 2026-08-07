#!/usr/bin/env bash
# =============================================================================
# Behavioural Anti-Fraud — production watchdog (always-on).
#
# Reports to Slack (with the actual cause) when:
#   * a watched container dies or goes unhealthy  — with its last log lines, or
#   * an ERROR / retrain-failure line appears in the service audit log
#     (e.g. a customer's behaviour changed and the profile retrain FAILED).
#
# Everything is logged to ./logs/watchdog.log. Slack is optional (BP_SLACK_WEBHOOK_URL);
# until it is set the watchdog still runs and logs — it just doesn't post.
#
# Run it alongside the stack (systemd unit / `nohup ./watchdog.sh &` / a sidecar).
#   WATCH_CONTAINERS  space-separated names (default: the three prod containers)
#   AUDIT_LOG         service audit log to tail (default ./logs/behaviour.log)
# =============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"

LOGDIR="${WATCHDOG_LOG_DIR:-./logs}"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/watchdog.log"
WATCH_CONTAINERS="${WATCH_CONTAINERS:-behaviour-profile-db adhere-behaviour adhere-behaviour-sync}"
AUDIT_LOG="${AUDIT_LOG:-./logs/behaviour.log}"
SLACK="${BP_SLACK_WEBHOOK_URL:-}"
[ -f .env ] && { set -a; . ./.env; set +a; SLACK="${BP_SLACK_WEBHOOK_URL:-$SLACK}"; }

log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
slack(){ [ -n "$SLACK" ] && curl -s -X POST -H 'Content-type: application/json' \
           --data "{\"text\":\"$(printf '%s' "$1" | sed 's/"/\\"/g' | tr '\n' ' ')\"}" "$SLACK" >/dev/null 2>&1 || true; }

log "watchdog: watching containers [$WATCH_CONTAINERS]; tailing $AUDIT_LOG"
[ -n "$SLACK" ] || log "watchdog: BP_SLACK_WEBHOOK_URL not set — alerts are logged only (set it later)."

# --- (A) container die / unhealthy events ------------------------------------
watch_containers(){
  docker events --filter 'event=die' --filter 'event=health_status' \
    --format '{{.Actor.Attributes.name}}|{{.Status}}|{{.Actor.Attributes.exitCode}}' 2>/dev/null \
  | while IFS='|' read -r name status code; do
      case " $WATCH_CONTAINERS " in
        *" $name "*)
          if [ "$status" = "die" ] || echo "$status" | grep -qi unhealthy; then
            cause="$(docker logs --tail 25 "$name" 2>&1 | tr '\n' ' ' | tail -c 900)"
            log "ALERT container=$name status=$status exit=${code:-?} :: $cause"
            slack ":rotating_light: Container *$name* $status (exit=${code:-?}). Cause (last logs): $cause"
          fi ;;
      esac
    done
}

# --- (B) service ERROR / retrain-failure lines -------------------------------
watch_audit(){
  [ -f "$AUDIT_LOG" ] || : > "$AUDIT_LOG"
  tail -Fn0 "$AUDIT_LOG" 2>/dev/null \
  | grep --line-buffered -Ei 'ERROR|CRITICAL|retrain .*fail|profile .*fail|persist failed' \
  | while read -r line; do
      log "ALERT audit :: $line"
      slack ":warning: Behavioural service issue: ${line}"
    done
}

watch_containers & watch_audit &
wait
