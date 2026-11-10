#!/bin/bash
# 设备端整体验收脚本（在 M2 设备上运行）
P=http://127.0.0.1:8787
PY=/opt/py312/bin/python
j() { $PY -c "import json,sys;d=json.load(sys.stdin);print($1)"; }
code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }

echo "=== T1 基础状态 ==="
curl -s $P/api/status | j "\"ready=%s can_feed=%s count=%s/%s auto=%s\"%(d['feeder_ready'],d['feeder_status']['can_feed'],d['daily_count'],d['daily_limit'],d['auto_enabled'])"

echo "=== T2 视频流 10s ==="
timeout 12 curl -s $P/api/live/mjpeg -o /tmp/live.mjpeg
$PY - <<'PY'
data = open('/tmp/live.mjpeg', 'rb').read()
print("mjpeg bytes=%d jpeg_frames=%d" % (len(data), data.count(b'\xff\xd8\xff')))
PY
curl -s $P/api/live/status | j "\"preview_fps=%.2f inference_fps=%.2f inference_ms=%.1f seq=%s err=%s\"%(d['preview_fps'],d['inference_fps'],d['inference_ms'],d['frame_sequence'],d['last_error'])"

echo "=== T3 设置上限/间隔 ==="
curl -s -X PUT $P/api/settings/feeding -H 'Content-Type: application/json' \
  -d '{"daily_limit":3,"cooldown_sec":30,"reset_on_start":false}' \
  | j "\"limit=%s cooldown=%s reset=%s\"%(d['daily_limit'],d['cooldown_sec'],d['reset_on_start'])"
echo "-- 非法值应 422 --"
for v in 0 101 '"x"' true; do
  printf "  limit=%s -> %s\n" "$v" "$(code -X PUT $P/api/settings/feeding -H 'Content-Type: application/json' -d "{\"daily_limit\":$v}")"
done

echo "=== T4 先重置拿干净计数 ==="
curl -s -X POST $P/api/records/reset -H 'Content-Type: application/json' -d '{"confirmed":true}' \
  | j "\"count=%s auto=%s limit=%s cooldown=%s\"%(d['daily_count'],d['auto_enabled'],d['daily_limit'],d['cooldown_sec'])"

echo "=== T5 冷却间隔拦截 ==="
curl -s -X POST $P/api/feed/manual -H 'Content-Type: application/json' -d '{"reason":"acc-1"}' \
  | j "\"fed=%s reason=%s\"%(d['fed'],d['reason'])"
curl -s -X POST $P/api/feed/manual -H 'Content-Type: application/json' -d '{"reason":"acc-2","bypass_cooldown":true}' \
  | j "\"fed=%s reason=%s\"%(d['fed'],d['reason'])"

echo "=== T6 每日上限拦截 (cooldown=0, limit=3) ==="
curl -s -X PUT $P/api/settings/feeding -H 'Content-Type: application/json' -d '{"cooldown_sec":0,"daily_limit":3}' >/dev/null
for n in 1 2 3 4; do
  curl -s -X POST $P/api/feed/manual -H 'Content-Type: application/json' -d "{\"reason\":\"acc-cap-$n\",\"bypass_cooldown\":true}" \
    | j "\"feed$n fed=%s reason=%s\"%(d['fed'],d['reason'])"
  sleep 0.1
done
curl -s $P/api/status | j "\"count=%s/%s limit_reached=%s remaining=%s\"%(d['daily_count'],d['daily_limit'],d['limit_reached'],d['remaining_feeds'])"

echo "=== T7 自动模式开关 ==="
curl -s -X POST $P/api/mode -H 'Content-Type: application/json' -d '{"auto_enabled":false}' | j "d"
curl -s -X POST $P/api/mode -H 'Content-Type: application/json' -d '{"auto_enabled":true}' | j "d"

echo "=== T8 重置需确认 ==="
printf "  未确认 -> %s\n" "$(code -X POST $P/api/records/reset -H 'Content-Type: application/json' -d '{"confirmed":false}')"
curl -s -X POST $P/api/records/reset -H 'Content-Type: application/json' -d '{"confirmed":true}' \
  | j "\"count=%s auto=%s limit=%s cooldown=%s\"%(d['daily_count'],d['auto_enabled'],d['daily_limit'],d['cooldown_sec'])"

echo "=== T9 事件分页 ==="
curl -s "$P/api/events?limit=3" | j "\"events=%d\"%len(d['events'])"

echo "=== T10 页面与接口可用性 ==="
printf "  GET / -> %s\n" "$(code $P/)"
printf "  GET /api/status -> %s\n" "$(code $P/api/status)"
printf "  GET /api/live/status -> %s\n" "$(code $P/api/live/status)"
printf "  POST /api/detections(非法) -> %s\n" "$(code -X POST $P/api/detections -H 'Content-Type: application/json' -d '{"label":"cat","score":2}')"

echo "=== T11 自动模式在时间窗外不投喂 ==="
curl -s $P/api/status | j "\"windows=%s\"%(d['active_time_windows'],)"
curl -s -X POST $P/api/records/reset -H 'Content-Type: application/json' -d '{"confirmed":true}' >/dev/null
curl -s -X PUT $P/api/settings/feeding -H 'Content-Type: application/json' -d '{"cooldown_sec":0,"daily_limit":10}' >/dev/null
curl -s -X POST $P/api/mode -H 'Content-Type: application/json' -d '{"auto_enabled":true}' >/dev/null
for i in 1 2 3; do
  curl -s -X POST $P/api/detections -H 'Content-Type: application/json' \
    -d '{"source":"manual","label":"cat","score":0.93,"bbox":[0.4,0.4,0.6,0.7]}' \
    | j "\"hit fed=%s reason=%s\"%(d['fed'],d['reason'])"
  sleep 0.2
done
curl -s $P/api/status | j "\"count=%s\"%(d['daily_count'],)"

echo "=== T12 自动模式在时间窗内可投喂（临时放宽窗口验证后恢复）==="
echo "  跳过：已在专项测试中验证 800->2200->800 且计数+1"
