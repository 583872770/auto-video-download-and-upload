# 部署修复说明

**更新时间**: 2026-10-30 10:30  
**问题**: Docker 容器启动失败 `ModuleNotFoundError`  
**影响**: 代码重构后首次部署

---

## ❌ 问题原因

在代码重构中创建了 `app/` 目录，但 `.dockerignore` 使用白名单模式，新目录未被包含到 Docker 构建上下文中。

### 错误信息
```
[服务启动] 服务初始化失败。
{"type": "ModuleNotFoundError", "location": ["deployment.py:214", "app.py:81"]}
```

### 缺失的模块
- `app/__init__.py`
- `app/extensions.py`
- `app/helpers.py`
- `app/routes/__init__.py`
- `app/routes/health.py`
- `app/routes/disk.py`

---

## ✅ 修复方式

已在 `.dockerignore` 添加白名单：

```diff
!web_worker.py
+!app/
+!app/**
!Utils/
```

**提交**: c4d6701

---

## 🚀 重新部署步骤

### 方式 1: 完整重建（推荐）

```bash
# 停止容器
docker compose -f compose.remote-mysql.yaml down

# 重新构建镜像（包含新的 app/ 目录）
docker compose -f compose.remote-mysql.yaml build

# 启动容器
docker compose -f compose.remote-mysql.yaml up -d

# 查看日志确认启动成功
docker compose -f compose.remote-mysql.yaml logs -f
```

### 方式 2: 一步完成

```bash
docker compose -f compose.remote-mysql.yaml up -d --build
```

---

## ✅ 验证成功

启动日志应该显示：

```
[服务启动] 正在加载视频工作台…
[服务启动] 正在启动浏览器桌面环境…
[服务启动] 正在连接 MySQL 并检查表结构…
[服务启动] 数据库已就绪，正在初始化素材库与实例配置…
[服务启动] 初始化完成，服务已就绪。  ← 看到这行表示成功
```

---

## 📝 相关提交

| 提交 | 说明 |
|------|------|
| ac41232 | refactor: 代码结构整理和模块化（创建 app/ 目录）|
| c4d6701 | fix: 添加 app/ 目录到 Docker 构建上下文 |

---

## 🔍 本地测试（可选）

如果想在本地验证修复：

```bash
# 检查 Docker 构建上下文是否包含 app/ 目录
docker build -t test-context . --no-cache --progress=plain 2>&1 | grep -E "(app/|COPY)"

# 或者构建后检查容器内是否有文件
docker build -t test-image .
docker run --rm test-image ls -la /app/app/
```

应该看到：
```
drwxr-xr-x  app/
-rw-r--r--  app/__init__.py
-rw-r--r--  app/extensions.py
-rw-r--r--  app/helpers.py
drwxr-xr-x  app/routes/
```

---

## ⚠️ 注意事项

1. **必须重新构建镜像**，仅重启容器无效
2. `.dockerignore` 改动后，Docker 不会自动检测，必须显式 `--build`
3. 如果用 CI/CD，确保拉取最新代码（包含 c4d6701）

---

## 🎯 后续优化（可选）

为避免类似问题，可以考虑：

1. **改进 .dockerignore**:
   ```
   # 改为黑名单模式（更简单）
   node_modules/
   .git/
   __pycache__/
   *.pyc
   *.log
   config.yaml
   cookies/
   subscriptions/
   database/
   media/
   ```

2. **添加构建测试**:
   - CI/CD 中测试 Docker 镜像能否正常导入所有模块
   - 本地提交前运行 `docker build --target test`

---

**修复完成，重新构建即可部署！** 🚀
