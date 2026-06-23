# Troubleshooting Guide

## Common Issues

### 1. LLM API Key Not Found
`ash
echo "sk-your-key-here" > config/api_keys.txt
# or
export DASHSCOPE_API_KEYS="sk-your-key-here"
`

### 2. Port Already in Use
`ash
# Windows
netstat -ano | findstr :6090
taskkill /PID <pid> /F

# Linux/Mac
lsof -ti:6090 | xargs kill -9
`

### 3. Vector DB Connection Failed
`ash
# Qdrant
docker run -p 6333:6333 qdrant/qdrant

# Milvus
docker compose -f docker-compose-milvus.yml up -d
`

### 4. HarmonyOS App Can't Connect
- Emulator: http://10.0.2.2:6090
- Real device: http://192.168.x.x:6090
- Edit harmony_app/entry/src/main/ets/utils/Constants.ets