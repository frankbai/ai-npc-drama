# 部署到 Render（免费方案）

部署完会得到一个形如 `https://ai-npc-drama-xxxx.onrender.com` 的网址，别人输入体验口令就能玩。Render 部分全程免费，只有调用模型的费用。大约需要 15 分钟。

## 先了解免费方案的限制

| 限制 | 影响 | 对策 |
|---|---|---|
| 15 分钟没人访问，服务就休眠 | 下一个打开的人要等大约 1 分钟才进得去 | 告诉大家"第一次打开慢是正常的"。数据存在数据库里，休眠不会丢 |
| 免费数据库 **30 天后到期**，再过 14 天删除 | 一个月后对话和记忆会没了 | 到期前用管理员身份导出数据（见文末）；想继续玩就新建一个免费库，或者升级成付费数据库 |
| 每月 750 小时免费运行时间 | 一个服务全天开着也用不完 | 无需处理 |
| 国内能否稳定打开 `onrender.com` 不确定 | 可能有人打不开或很慢 | 部署好后先找一位不用代理的人试一下 |

## 你需要准备

1. 一个 GitHub 账号。
2. 一个 Render 账号：在 render.com 用 GitHub 账号直接登录即可。
3. 模型服务的**密钥**、**地址**和**模型名**：
   - 用 OpenAI 官方：密钥在 OpenAI 后台创建，地址是 `https://api.openai.com/v1`。账户里要有余额，建议设一个月度消费上限。
   - 用中转服务：向提供方要密钥和地址。任何兼容 OpenAI Chat Completions 接口的服务都可以。
4. 想好两个口令：
   - **体验口令**（`ACCESS_CODE`）：发给来玩的人，可以是中文，比如 `风雪夜归人`；
   - **管理员口令**（`ADMIN_CODE`）：只有你自己知道，必须和体验口令不同。

## 第一步：把代码放到 GitHub

如果你是从 GitHub 上看到这个仓库的，直接 **Fork** 一份到自己的账号下即可，跳到第二步。

如果代码在你电脑上：

```bash
cd ai-npc-drama
git init && git add . && git commit -m "init"
git branch -M main
git remote add origin https://github.com/<你的账号>/ai-npc-drama.git
git push -u origin main
```

注意：`local-data/` 已经写在 `.gitignore` 里，不会被上传——那里面有你的密钥和本机的存档。

> 仓库是公开的，就意味着任何人都能看到剧情的全部秘密（`content/` 里写着凶手是谁）。如果你改成了自己不想剧透的故事，仓库设为 Private 即可，Render 一样能部署。

## 第二步：在 Render 创建

1. 打开 dashboard.render.com。第一次登录会有两页问卷，随便选或者点 **Skip Survey**。
2. 点右上角 **New** → **Blueprint**。
3. 第一次需要 **Connect GitHub**，授权 Render 访问刚才的仓库。选中这个仓库。
4. 在 "You are deploying from a Blueprint" 页面填写：
   - **Blueprint Name**：`ai-npc-drama`；
   - **Branch**：保持 `main`；
   - **Blueprint Path**：空着；
   - 下面会列出 "Create database ai-npc-drama-db" 和 "Create web service ai-npc-drama"，以及五个要填的值：

   | Key | 填什么 |
   |---|---|
   | `OPENAI_API_KEY` | 模型服务的密钥 |
   | `NPC_BASE_URL` | 模型服务地址，OpenAI 官方填 `https://api.openai.com/v1` |
   | `NPC_MODEL` | 模型名，以你的模型服务实际支持的为准 |
   | `ACCESS_CODE` | 体验口令 |
   | `ADMIN_CODE` | 管理员口令 |

5. 点页面底部的 **Deploy Blueprint**（也可能叫 **Apply**）。
6. 等几分钟。在 Blueprint 页面点紫色的 **ai-npc-drama**，或者在 Render 首页点类型为 Web Service 的 **ai-npc-drama**，看到状态变成 **Live** 就好了。
7. 网址在服务页面左上方，形如 `https://ai-npc-drama-xxxx.onrender.com`。

如果状态是 **Failed**，点左侧 **Logs** 看最后几十行报错。

## 第三步：自己先试

1. 打开网址，第一次可能要等半分钟到一分钟。看到口令框后，输入**管理员口令**。
2. 新建一个存档，看完第一章的 CG，去天字房找谢不归聊几句。回复应该是有具体内容的对白；如果一直是"你这么一说，倒让我想起点别的"这类固定句子，说明还在模拟模式，检查密钥。
3. 如果回复里出现"模型服务返回 4xx"，对照 [配置说明](配置说明.md) 末尾的"常见问题"调整。
4. 点右上角 **用量**，能看到调用次数和估算费用，就说明一切正常。

## 第四步：发给别人

可以参考这段：

> 我做了一个 AI NPC 的小 Demo，想请你玩一玩：
> 网址：https://ai-npc-drama-xxxx.onrender.com
> 体验口令：（你的体验口令）
> 第一次打开如果要等一分钟，是服务器在醒来，不是坏了。进去后给自己和你的客栈取个名字，故事就开始了。每一章先看一段剧情，然后你有一盏灯的时间去找镇上的人说话（三个人共 12 句），做点经营和调查。一共六章，大约一个小时，最后要当众指认凶手。他们会记得你说过的话，也会互相传话——每个人的故事都不一样。
> 聊天内容会保存下来，请不要输入真实的隐私信息。

## 访客和管理员的区别

| | 访客（体验口令） | 管理员（管理员口令） |
|---|---|---|
| 存档 | 只看到自己建的，最多 5 个 | 能看到所有人的，标着"别人的" |
| 进入下一段时日 | 看完本章剧情、和任意一人说过话后，自己推进，只影响自己 | 同左 |
| 心智面板 | 默认看不到（可用 `SHOW_MIND=1` 打开；会剧透） | 能看，还能查看完整提示词 |
| 模型设置、用量、导出数据 | 不能 | 可以 |
| 限流 | 每分钟 10 句、每天 300 句 | 不限 |

## 以后要改设置

Render 首页 → 点 Web Service **ai-npc-drama** → 左侧 **Environment** → 改完点 **Save changes**（或 **Save and deploy**），服务会自动重启。每个变量的含义见 [配置说明](配置说明.md)。

## 更新代码

推到 GitHub 的 `main` 分支，Render 会自动重新部署，一两分钟后生效。线上已有的数据会保留。

## 30 天到期前

1. 用管理员口令登录 → **用量** → **导出全部数据**，保存这个 JSON 文件，里面有所有对话、日记和记忆。
2. 想继续玩：在 Render 删除旧数据库，再 **New → Postgres** 建一个 Free 的（区域要和服务一样），把 `ai-npc-drama` 的 Environment 里的 `DATABASE_URL` 换成新库的 **Internal Database URL**。所有存档会从头开始。
