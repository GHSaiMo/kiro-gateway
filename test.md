export PROXY_API_KEY="kirogateway"
curl -s http://localhost:8000/v1/models \
  -H "Authorization: Bearer ${PROXY_API_KEY}" | jq


curl http://localhost:8000/health




curl http://localhost:8000/v1/messages \
  -H "x-api-key: kirogateway" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'