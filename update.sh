#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail

INSTALL_DIR="${HOME}/video-downloader"

if ! command -v git >/dev/null 2>&1; then
    echo "خطأ: Git غير مثبت. نفّذ: pkg install git"
    exit 1
fi

if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo "خطأ: لم أجد نسخة Git من المشروع في:"
    echo "  $INSTALL_DIR"
    echo "إذا لم تثبّت المشروع بعد، شغّل install.sh أولًا."
    exit 1
fi

cd "$INSTALL_DIR"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "توقّف التحديث: لديك تعديلات محلية على ملفات Git المتتبّعة."
    echo "احفظ تعديلاتك أو انسخها احتياطيًا ثم أعد تشغيل update.sh."
    echo "لن أحذف تعديلاتك أو أستبدلها تلقائيًا."
    exit 1
fi

echo "[1/3] جلب معلومات التحديث..."
git fetch --prune

BRANCH="$(git branch --show-current)"
if [ -z "$BRANCH" ]; then
    echo "خطأ: النسخة الحالية ليست على فرع Git عادي."
    exit 1
fi

UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
if [ -z "$UPSTREAM" ]; then
    echo "خطأ: لا يوجد upstream مضبوط للفرع '$BRANCH'."
    echo "تحقق من إعدادات مستودع Git قبل المتابعة."
    exit 1
fi

if [ "$(git rev-parse HEAD)" = "$(git rev-parse "$UPSTREAM")" ]; then
    echo "المشروع محدّث بالفعل."
else
    echo "[2/3] تثبيت تحديثات الملفات..."
    if ! git merge-base --is-ancestor HEAD "$UPSTREAM"; then
        echo "تعذّر التحديث الآمن: الفرع المحلي متباعد عن $UPSTREAM."
        echo "لم أجرِ reset أو أحذف أي ملفات. يلزم فحص حالة Git يدويًا."
        exit 1
    fi
    git merge --ff-only "$UPSTREAM"
fi

echo "[3/3] تثبيت/تحديث مكتبات Python المطلوبة..."
python -m pip install -r requirements.txt

echo
echo "اكتمل التحديث. شغّل التطبيق مجددًا بالأمر: video"
echo "إذا كان التطبيق يعمل حاليًا، أوقفه بـ Ctrl+C ثم شغّله من جديد."
