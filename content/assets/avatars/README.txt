把头像放在这里（正方形，512×512，头部居中）。没有图片时，页面显示代码画的临时头像。
提示词见 docs/美术提示词.md。
格式 png / jpg / webp 都行，文件名（不含扩展名）对上就会被认出来。仓库里用的是 webp，体积小、加载快。

NPC 表情头像：<NPC id>_<表情键>.png，表情键见各自的 content/npcs/<id>/npc.json
  xie_bugui_calm / angry / stubborn / smile / stunned / sad
  he_rendong_calm / wary / smile / angry / worried / sad
  yan_shouzheng_calm / stern / smile / cold
玩家预设头像：me_1.png … me_6.png（见 content/pack.json 的 player.avatars）
