export PROXY_API_KEY="kirogateway"
curl -s http://localhost:8000/v1/models \
  -H "Authorization: Bearer ${PROXY_API_KEY}" | jq


curl http://localhost:8000/health
