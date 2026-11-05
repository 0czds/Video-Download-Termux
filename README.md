# Local Video Downloader - LVD

برنامج يستخدم YT-dlp و ffmpeg عبر Termux لتحميل الفيديوهات والمقاطع الصوتية من مواقع التواصل الاجتماعي

يدعم المشروع حاليًا ثلاث منصات: **YouTube**، **TikTok**، و**Instagram**.

## التنزيل والتثبيت

تحتاج الى تنزيل حزم yt-dlp و ffmpeg و python. ثم تحميل المشروع كاملا بهذا الامر

```bash
curl -fsSL https://raw.githubusercontent.com/0czds/Video-Download-Termux/main/install.sh | bash
```

## التشغيل في Termux

```bash
pkg update
pip install -r requirements.txt
python app.py
```

افتح المتصفح على:

`http://127.0.0.1:5000`

## تحديث نسخة مثبتة مسبقًا في Termux

لتنزيل اصدار جديد بعد نزول اي تحديثات. استخدم هذا الامر:

```bash
bash ~/video-downloader/update.sh
```

## المنصات المدعومة والروابط المقبولة

النظام يتعرف على المنصة من نطاق الرابط نفسه (وليس من كلمات داخل الرابط)، عبر قائمة نطاقات مسموحة صريحة لكل منصة. أي نطاق خارج هذه القوائم يُرفض برسالة "الرابط غير صالح أو المنصة غير مدعومة."، حتى لو كان yt-dlp يدعمه تقنيًا.

**YouTube** — `youtube.com`، `www.youtube.com`، `m.youtube.com`، `music.youtube.com`، `youtu.be`، `youtube-nocookie.com`
مثال: `https://www.youtube.com/watch?v=XXXXXXXXXXX` أو `https://youtu.be/XXXXXXXXXXX`

**TikTok** — `tiktok.com`، `www.tiktok.com`، `m.tiktok.com`، `vm.tiktok.com`، `vt.tiktok.com`
مثال: `https://www.tiktok.com/@username/video/1234567890123456789`

**Instagram** — `instagram.com`، `www.instagram.com`، `instagr.am`، `www.instagr.am`
مثال: `https://www.instagram.com/reel/XXXXXXXXXXX/` (Reels والمنشورات المفردة التي تحتوي فيديو)

يمكن تخصيص أي من هذه القوائم عبر متغيرات البيئة `YOUTUBE_ALLOWED_HOSTS`، `TIKTOK_ALLOWED_HOSTS`، `INSTAGRAM_ALLOWED_HOSTS` (قيم مفصولة بفواصل).


## مجلد التنزيلات

افتراضيًا تُحفظ الملفات في:

`/storage/emulated/0/Download/VideoDownloader`

وهذا مجلد مخصص للمشروع حتى لا يعرض السيرفر ملفات أخرى موجودة في مجلد Downloads العام. يمكن تغيير المسار عبر متغير البيئة `DOWNLOADS_DIR`.

### اختيار YouTube player clients

افتراضيًا لا يفرض المشروع أي `player_client` حتى يترك yt-dlp يختار عملاء YouTube المناسبين تلقائيًا. هذا مهم لأن توفر الصيغ ومتطلبات PO Token تتغير مع الوقت. لا تستخدم `YTDLP_PLAYER_CLIENTS` إلا عند وجود سبب محدد للاختبار أو التخصيص.

```bash
YTDLP_PLAYER_CLIENTS="default,web_embedded" python app.py
```

لإتاحة السيرفر من جهاز آخر على الشبكة المحلية فقط عند الحاجة:

```bash
FLASK_HOST=0.0.0.0 python app.py
```

الافتراضي هو `127.0.0.1` لتقليل التعرض غير المقصود على الشبكة.

## جودة الفيديو في الواجهة

تعرض الواجهة ستة مستويات أساسية فقط: `144p` و`240p` و`360p` و`480p` و`720p` و`1080p`. يتم اختيار أفضل stream فعلي لكل مستوى من الصيغ التي ترجعها المنصة (YouTube، TikTok، أو Instagram)، مع تفضيل معدل fps أعلى عند توفره (مثل 1080p 60 FPS).

بعض فيديوهات TikTok وInstagram لا تحتوي أصلًا أيًا من هذه الدقات القياسية. في هذه الحالة فقط، تعرض الواجهة الجودة (أو الجودات) الفعلية المتاحة للفيديو بارتفاعها الحقيقي (مثل `640p`)، مع توضيح أنها "أقرب جودة متاحة" وليست إحدى الدقات القياسية الست.