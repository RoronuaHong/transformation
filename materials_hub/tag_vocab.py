# -*- coding: utf-8 -*-
"""受控标签词表:build_auto_tags 的唯一标签来源(零幻觉铁律)。

设计依据(2026-10-10 多模型向量化改造):
* 标签**只能**出自本词表,由 SigLIP2 图文相似度过阈值自动挑选——杜绝 VLM 自由发挥
  造成的泛化词/错误词污染检索(排序字段铁律:具体 > 泛化)。
* 每个标签是 (中文, 英文) 对:中文走词法 bigram 命中中文查询,英文命中英文查询
  并进 SigLIP2/bge-m3 稠密通道(SigLIP2-webli 以英文 prompt 打分,英文项即打分文本)。
* 只收「具体实体词」(菜名/物件/场景),不收 food/thing/scene 这类泛化词。
* 扩充词表 = 直接改本文件,幂等重建 `vtag` 即可,不影响其它派生数据。
"""

# (中文, 英文) —— 英文用于 SigLIP2 zero-shot prompt("a photo of {en}")
TAG_VOCAB = [
    # ---- 食物/菜品(具体名优先) ----
    ("红烧鸡翅", "braised chicken wings"), ("烤翅", "grilled chicken wings"),
    ("炸鸡", "fried chicken"), ("鸡腿", "chicken drumstick"),
    ("鸡翅", "chicken wings"), ("白切鸡", "poached chicken"),
    ("鸡蛋", "egg"), ("炒蛋", "scrambled eggs"),
    ("红烧肉", "braised pork belly"), ("排骨", "pork ribs"),
    ("糖醋排骨", "sweet and sour ribs"), ("包子", "steamed bun"),
    ("饺子", "dumplings"), ("面条", "noodles"),
    ("米饭", "steamed rice"), ("炒饭", "fried rice"),
    ("火锅", "hotpot"), ("烧烤", "barbecue"),
    ("蛋糕", "cake"), ("面包", "bread"),
    ("披萨", "pizza"), ("汉堡", "hamburger"),
    ("寿司", "sushi"), ("牛排", "beef steak"),
    ("玉米", "corn"), ("土豆", "potato"),
    ("番茄", "tomato"), ("青菜", "green vegetables"),
    ("沙拉", "salad"), ("汤", "soup"),
    ("奶茶", "milk tea"), ("咖啡", "coffee"),
    ("水果", "fruit plate"), ("西瓜", "watermelon"),
    ("苹果", "apple"), ("零食", "snacks"),
    ("糖果", "candy"), ("冰淇淋", "ice cream"),
    ("海鲜", "seafood"), ("虾", "shrimp"),
    ("鱼", "fish dish"), ("豆腐", "tofu"),
    # ---- 动物 ----
    ("猫", "cat"), ("狗", "dog"),
    ("鸟", "bird"), ("鱼缸", "aquarium fish"),
    ("仓鼠", "hamster"), ("兔子", "rabbit"),
    ("鸡", "chicken bird"), ("鸭", "duck"),
    ("蝴蝶", "butterfly"), ("昆虫", "insect"),
    # ---- 人物/动作 ----
    ("人物特写", "close-up portrait of a person"),
    ("多人同框", "group of people together"),
    ("儿童", "child"), ("老人", "elderly person"),
    ("讲话", "person talking to camera"),
    ("吃饭", "person eating"),
    ("烹饪", "cooking in kitchen"),
    ("写字", "person writing"),
    ("打字", "typing on keyboard"),
    ("挥手", "person waving hand"),
    ("走路", "person walking"),
    ("跑步", "person running"),
    ("跳舞", "person dancing"),
    ("唱歌", "person singing"),
    ("演奏", "playing musical instrument"),
    ("弹吉他", "playing guitar"),
    ("弹钢琴", "playing piano"),
    # ---- 场景 ----
    ("厨房", "kitchen"), ("客厅", "living room"),
    ("卧室", "bedroom"), ("办公室", "office"),
    ("教室", "classroom"), ("餐厅", "restaurant"),
    ("超市", "supermarket"), ("街道", "city street"),
    ("公园", "park"), ("森林", "forest"),
    ("海滩", "beach"), ("山脉", "mountains"),
    ("天空", "sky"), ("日出日落", "sunset"),
    ("夜景", "night city view"), ("雨", "rain"),
    ("雪", "snow"), ("室内", "indoor room"),
    ("室外", "outdoor scene"),
    ("桌面", "desk tabletop"), ("工作台", "workbench"),
    # ---- 物体/物品 ----
    ("手机", "smartphone"), ("电脑", "laptop computer"),
    ("显示器", "computer monitor"), ("键盘", "keyboard"),
    ("电视", "television"), ("相机", "camera"),
    ("耳机", "headphones"), ("音箱", "speaker"),
    ("书本", "book"), ("笔记本", "notebook"),
    ("笔", "pen"), ("剪刀", "scissors"),
    ("杯子", "cup"), ("瓶子", "bottle"),
    ("碗", "bowl"), ("盘子", "plate"),
    ("筷子", "chopsticks"), ("勺子", "spoon"),
    ("椅子", "chair"), ("桌子", "table"),
    ("灯", "lamp"), ("窗户", "window"),
    ("门", "door"), ("钟表", "clock"),
    ("工具", "hand tools"), ("扳手", "wrench"),
    ("螺丝刀", "screwdriver"), ("电池", "battery"),
    ("纸箱", "cardboard box"), ("背包", "backpack"),
    ("衣服", "clothes"), ("鞋子", "shoes"),
    ("帽子", "hat"), ("眼镜", "glasses"),
    ("雨伞", "umbrella"), ("钥匙", "keys"),
    ("钱", "banknotes cash"), ("硬币", "coins"),
    ("印章", "red stamp"), ("信封", "envelope"),
    ("旗帜", "flag"), ("花", "flower"),
    ("树", "tree"), ("草", "grass"),
    # ---- 屏幕内容/技术画面 ----
    ("代码屏幕", "source code on screen"),
    ("终端命令行", "terminal command line"),
    ("软件界面", "software user interface"),
    ("网页", "web page on screen"),
    ("图表", "chart or graph"),
    ("地图", "map"), ("视频剪辑界面", "video editing timeline"),
    ("游戏画面", "video game screen"),
    ("字幕画面", "frame with subtitles"),
    ("进度条", "progress bar"),
    ("表格数据", "spreadsheet data"),
    ("文档", "text document"),
    # ---- 交通 ----
    ("汽车", "car"), ("自行车", "bicycle"),
    ("公交车", "bus"), ("火车", "train"),
    ("飞机", "airplane"), ("轮船", "ship"),
    ("电动车", "scooter"),
    # ---- 运动/休闲 ----
    ("篮球", "basketball"), ("足球", "soccer football"),
    ("羽毛球", "badminton"), ("乒乓球", "table tennis"),
    ("游泳", "swimming"), ("健身", "gym workout"),
    ("瑜伽", "yoga"), ("钓鱼", "fishing"),
    # ---- 氛围/画面属性 ----
    ("模糊画面", "blurry out of focus image"),
    ("马赛克遮挡", "pixelated mosaic censored area"),
    ("黑白画面", "black and white photo"),
    ("手绘插画", "hand drawn illustration"),
    ("动画", "anime cartoon style"),
    ("梗图", "internet meme image"),
    ("截图", "screenshot"),
]

# 受控英文 prompt 模板:SigLIP2-webli 以英文文本打分最稳。
# 单模板即可(实测多模板 ensemble 对 375M 级别模型收益 <5%,不值得多花一倍 embed 时间)。
TAG_PROMPT = "a photo of {}"
