"""Default client-facing texts for an education centre (RU / KZ / UZ-latin).

Short, warm, one question per message — the same style the agent prompt asks
for.  These are what parents and students see outside a dialogue (reminders,
payment ladder, absences), where no model is involved at all.
"""
from __future__ import annotations

DEFAULTS: dict[str, dict[str, str]] = {
    # ---- consent ---------------------------------------------------------
    "consent_request": {
        "ru": "Чтобы записать, нужно согласие на обработку персональных данных "
        "(имя и телефон — только для записи). Подтверждаете?",
        "kz": "Жазу үшін дербес деректерді өңдеуге келісім қажет "
        "(аты және телефон — тек жазылу үшін). Растайсыз ба?",
        "uz": "Yozib qo'yish uchun shaxsiy ma'lumotlarni qayta ishlashga rozilik kerak "
        "(ism va telefon — faqat navbat uchun). Tasdiqlaysizmi?",
    },
    "consent_thanks": {
        "ru": "Спасибо! Записываю.",
        "kz": "Рақмет! Жазып жатырмын.",
        "uz": "Rahmat! Yozib qo'yaman.",
    },
    # ---- trial lessons ---------------------------------------------------
    "trial_confirmed": {
        "ru": "Записала {student} на пробное: {course}, {when}.\n"
        "Адрес: {address}\n{map_line}{bring_line}До встречи!",
        "kz": "{student} сынақ сабағына жазылды: {course}, {when}.\n"
        "Мекенжай: {address}\n{map_line}{bring_line}Кездескенше!",
        "uz": "{student} sinov darsiga yozildi: {course}, {when}.\n"
        "Manzil: {address}\n{map_line}{bring_line}Ko'rishguncha!",
    },
    "trial_cancelled": {
        "ru": "Отменила пробное на {when}. Если захотите вернуться — подберу новое время.",
        "kz": "{when} сынақ сабағы тоқтатылды. Қаласаңыз — жаңа уақыт таңдаймын.",
        "uz": "{when} sinov darsi bekor qilindi. Xohlasangiz — yangi vaqt tanlayman.",
    },
    "trial_reminder_24h": {
        "ru": "Напоминаю: {when} ждём {student} на пробное по «{course}».\nПридёте?",
        "kz": "Еске саламын: {when} {student} «{course}» сынақ сабағына күтеміз.\nКелесіз бе?",
        "uz": "Eslatma: {when} {student} ni «{course}» sinov darsiga kutamiz.\nKelasizmi?",
    },
    "trial_reminder_2h": {
        "ru": "Через пару часов ждём {student} — {when}. Адрес: {address}\n{map_line}",
        "kz": "Екі сағаттан кейін {student} күтеміз — {when}. Мекенжай: {address}\n{map_line}",
        "uz": "Ikki soatdan keyin {student} ni kutamiz — {when}. Manzil: {address}\n{map_line}",
    },
    "trial_rebook": {
        "ru": "Не получилось прийти на пробное? Бывает. Подобрать новое время для {student}? "
        "Есть {slots}.",
        "kz": "Сынақ сабағына келе алмадыңыз ба? Болады. {student} үшін жаңа уақыт таңдайын ба? "
        "{slots} бар.",
        "uz": "Sinov darsiga kelib ulgurmadingizmi? Bo'ladi. {student} uchun yangi vaqt "
        "tanlaymi? {slots} bor.",
    },
    # ---- trial follow-up (the main conversion point, ТЗ B5 У4) -----------
    "trial_followup_1": {
        "ru": "{student} сегодня был на пробном. Преподаватель: {teacher_note}\n"
        "Подходит группа «{group}»: {schedule}, {price}. {seats}\n"
        "Записать?",
        "kz": "{student} бүгін сынақ сабағында болды. Ұстаз: {teacher_note}\n"
        "«{group}» тобы қолайлы: {schedule}, {price}. {seats}\nЖазайын ба?",
        "uz": "{student} bugun sinov darsida bo'ldi. O'qituvchi: {teacher_note}\n"
        "«{group}» guruhi mos: {schedule}, {price}. {seats}\nYozamizmi?",
    },
    "trial_followup_2": {
        "ru": "Если ещё актуально — место в «{group}» пока свободно: {schedule}, {price}. "
        "{seats}\nЗакрепить за {student}?",
        "kz": "Әлі де қажет болса — «{group}» тобында орын бар: {schedule}, {price}. "
        "{seats}\n{student} үшін сақтайын ба?",
        "uz": "Hali ham kerak bo'lsa — «{group}» da joy bor: {schedule}, {price}. "
        "{seats}\n{student} uchun saqlaymi?",
    },
    "trial_followup_3": {
        "ru": "Последнее напоминание по «{course}»: группа «{group}» стартует по расписанию "
        "{schedule}. {seats}\nЕсли неактуально — напишите «не надо», больше не потревожу.",
        "kz": "«{course}» бойынша соңғы еске салу: «{group}» тобы {schedule} кестесімен "
        "басталады. {seats}\nҚажет болмаса — «керек емес» деп жазыңыз.",
        "uz": "«{course}» bo'yicha oxirgi eslatma: «{group}» guruhi {schedule} jadvali bilan "
        "boshlanadi. {seats}\nKerak bo'lmasa — «kerak emas» deb yozing.",
    },
    # ---- payments and debts (ТЗ B5 У6) -----------------------------------
    "pay_reminder_before": {
        "ru": "Напоминаю про оплату за {student}: {amount} {currency} за {period}, "
        "срок до {due}.\n{pay_line}",
        "kz": "{student} үшін төлем туралы еске саламын: {period} үшін {amount} {currency}, "
        "{due} дейін.\n{pay_line}",
        "uz": "{student} uchun to'lov haqida eslatma: {period} uchun {amount} {currency}, "
        "{due} gacha.\n{pay_line}",
    },
    "pay_reminder_due": {
        "ru": "Сегодня срок оплаты за {student}: {amount} {currency} за {period}.\n{pay_line}",
        "kz": "Бүгін {student} төлемінің мерзімі: {period} үшін {amount} {currency}.\n{pay_line}",
        "uz": "Bugun {student} to'lovi muddati: {period} uchun {amount} {currency}.\n{pay_line}",
    },
    "pay_reminder_overdue": {
        "ru": "Оплата за {student} за {period} ещё не поступила: {amount} {currency}, "
        "просрочка {days} дн.\n{pay_line}",
        "kz": "{student} үшін {period} төлемі келмеді: {amount} {currency}, "
        "кешігу {days} күн.\n{pay_line}",
        "uz": "{student} uchun {period} to'lovi kelmadi: {amount} {currency}, "
        "kechikish {days} kun.\n{pay_line}",
    },
    # ---- attendance ------------------------------------------------------
    "absence_notice": {
        "ru": "{student} не был(а) на занятии {when} ({group}). Если это по уважительной "
        "причине — напишите, отмечу.",
        "kz": "{student} {when} сабағында болмады ({group}). Себебі болса — жазыңыз.",
        "uz": "{student} {when} darsida bo'lmadi ({group}). Sabab bo'lsa — yozing.",
    },
    "lesson_cancelled": {
        "ru": "Занятие группы «{group}» {when} отменено. {reason}\nО новой дате сообщим.",
        "kz": "«{group}» тобының {when} сабағы тоқтатылды. {reason}\nЖаңа күнін хабарлаймыз.",
        "uz": "«{group}» guruhining {when} darsi bekor qilindi. {reason}\nYangi kunini aytamiz.",
    },
    "lesson_moved": {
        "ru": "Занятие группы «{group}» переносится с {when} на {new_when}. {reason}",
        "kz": "«{group}» тобының сабағы {when} күнінен {new_when} күніне ауысады. {reason}",
        "uz": "«{group}» guruhining darsi {when} dan {new_when} ga ko'chadi. {reason}",
    },
    # ---- lifecycle -------------------------------------------------------
    "group_starts": {
        "ru": "Группа «{group}» набрана ({taken} человек) и стартует {starts}. "
        "Расписание: {schedule}. До встречи!",
        "kz": "«{group}» тобы жиналды ({taken} адам) және {starts} басталады. "
        "Кесте: {schedule}. Кездескенше!",
        "uz": "«{group}» guruhi to'ldi ({taken} kishi) va {starts} boshlanadi. "
        "Jadval: {schedule}. Ko'rishguncha!",
    },
    "renew_reminder": {
        "ru": "Оплаченный период для {student} заканчивается {until}. Продлеваем занятия в "
        "«{group}»?",
        "kz": "{student} үшін төленген кезең {until} бітеді. «{group}» сабақтарын жалғастырамыз ба?",
        "uz": "{student} uchun to'langan davr {until} tugaydi. «{group}» darslarini davom "
        "ettiramizmi?",
    },
    "next_level_offer": {
        "ru": "{student} заканчивает уровень. Следующий — «{group}» ({level}): {schedule}, "
        "{price}, старт {starts}. Записать?",
        "kz": "{student} деңгейді бітіреді. Келесі — «{group}» ({level}): {schedule}, "
        "{price}, басталуы {starts}. Жазайын ба?",
        "uz": "{student} darajani tugatadi. Keyingisi — «{group}» ({level}): {schedule}, "
        "{price}, boshlanishi {starts}. Yozamizmi?",
    },
    "parent_report": {
        "ru": "Отчёт по {student} ({group}) за месяц:\nПосещаемость {present} из {total} "
        "({rate}%)\nУровень: {level}",
        "kz": "{student} ({group}) бойынша айлық есеп:\nҚатысу {present}/{total} "
        "({rate}%)\nДеңгей: {level}",
        "uz": "{student} ({group}) bo'yicha oylik hisobot:\nDavomat {present}/{total} "
        "({rate}%)\nDaraja: {level}",
    },
    "reactivation": {
        "ru": "{student}, открываем новый набор. Если захотите вернуться — подберу группу под "
        "ваш уровень. Написать «стоп», чтобы не получать такие сообщения.",
        "kz": "{student}, жаңа топ жинаймыз. Қайтып келгіңіз келсе — деңгейіңізге топ таңдаймын. "
        "Хабарламаларды тоқтату үшін «стоп» деп жазыңыз.",
        "uz": "{student}, yangi guruh to'playmiz. Qaytmoqchi bo'lsangiz — darajangizga guruh "
        "tanlayman. To'xtatish uchun «stop» deb yozing.",
    },
    # ---- follow-up on an unconverted enquiry ----------------------------
    "followup_1": {
        "ru": "Оставила за вами интерес к «{service}». Подобрать удобное время для пробного? "
        "Есть {slots}.",
        "kz": "«{service}» бойынша сұрауыңыз сақталды. Сынақ сабағына уақыт таңдайын ба? "
        "{slots} бар.",
        "uz": "«{service}» bo'yicha so'rovingiz saqlandi. Sinov darsiga vaqt tanlaymi? "
        "{slots} bor.",
    },
    "followup_2": {
        "ru": "Если ещё актуально — на этой неделе есть пробные: {slots}. Записать?",
        "kz": "Әлі де қажет болса — осы аптада сынақ сабақтары бар: {slots}. Жазайын ба?",
        "uz": "Hali ham kerak bo'lsa — bu hafta sinov darslari bor: {slots}. Yozamizmi?",
    },
    "followup_3": {
        "ru": "Последнее напоминание по «{service}»: {reason} "
        "Если неактуально — просто напишите «не надо», больше не потревожу.",
        "kz": "«{service}» бойынша соңғы еске салу: {reason} "
        "Қажет болмаса — «керек емес» деп жазыңыз.",
        "uz": "«{service}» bo'yicha oxirgi eslatma: {reason} "
        "Kerak bo'lmasa — «kerak emas» deb yozing.",
    },
    # ---- reviews ---------------------------------------------------------
    "study_review_request": {
        "ru": "{student} учится у нас месяц. Оцените от 1 до 5 — это помогает нам стать лучше.",
        "kz": "{student} бізде бір ай оқып жүр. 1-ден 5-ке дейін бағалаңыз.",
        "uz": "{student} bizda bir oy o'qiydi. 1 dan 5 gacha baho bering.",
    },
    "review_promote": {
        "ru": "Спасибо! Если не сложно, оставьте пару слов здесь — это очень помогает: {link}",
        "kz": "Рақмет! Мүмкін болса, осында пікір жазыңыз: {link}",
        "uz": "Rahmat! Imkon bo'lsa, shu yerda fikr qoldiring: {link}",
    },
    "review_apology": {
        "ru": "Спасибо за честность. Передам руководителю — с вами свяжутся.",
        "kz": "Шынайылығыңыз үшін рақмет. Жетекшіге жеткіземін.",
        "uz": "Rostgo'yligingiz uchun rahmat. Rahbarga yetkazaman.",
    },
    # ---- handoff ---------------------------------------------------------
    "handoff_notice": {
        "ru": "Подключаю администратора — он ответит здесь же.",
        "kz": "Әкімшіні қосамын — осы жерде жауап береді.",
        "uz": "Administratorni qo'shaman — shu yerda javob beradi.",
    },
    "handoff_night": {
        "ru": "Сейчас нерабочее время. Администратор ответит утром, после 09:00.",
        "kz": "Қазір жұмыс уақыты емес. Әкімші таңертең 09:00-ден кейін жауап береді.",
        "uz": "Hozir ish vaqti emas. Administrator ertalab 09:00 dan keyin javob beradi.",
    },
    # Kept for the shared runtime: in an education centre a "red flag" is a
    # safety issue with a child, not a medical emergency, so it goes to a human
    # at once and we never improvise.
    "red_flag": {
        "ru": "Это важно и срочно. {call_line}\nЯ уже передала администратору, "
        "он свяжется с вами.",
        "kz": "Бұл маңызды және шұғыл. {call_line}\nӘкімшіге жеткіздім.",
        "uz": "Bu muhim va shoshilinch. {call_line}\nAdministratorga yetkazdim.",
    },
    "photo_received": {
        "ru": "Фото передам администратору — он посмотрит и ответит здесь.",
        "kz": "Фотоны әкімшіге жеткіземін — ол қарап, осы жерде жауап береді.",
        "uz": "Rasmni administratorga yetkazaman — ko'rib, shu yerda javob beradi.",
    },
    # ---- system ----------------------------------------------------------
    "opt_out_done": {
        "ru": "Больше не буду писать по рассылкам. Если понадобимся — просто напишите.",
        "kz": "Жарнамалық хабарлама жібермеймін. Қажет болсақ — жазыңыз.",
        "uz": "Boshqa reklama xabar yubormayman. Kerak bo'lsak — yozing.",
    },
    "fallback": {
        "ru": "Не до конца поняла. Передаю администратору — он ответит здесь.",
        "kz": "Толық түсінбедім. Әкімшіге жеткіземін.",
        "uz": "To'liq tushunmadim. Administratorga yetkazaman.",
    },
    "bot_paused": {
        "ru": "Администратор уже в диалоге — он ответит здесь.",
        "kz": "Әкімші диалогта — осы жерде жауап береді.",
        "uz": "Administrator suhbatda — shu yerda javob beradi.",
    },
    "data_deleted": {
        "ru": "Запрос на удаление данных принят. Обработаем в течение 15 рабочих дней "
        "и напишем вам.",
        "kz": "Деректерді жою сұрауы қабылданды. 15 жұмыс күні ішінде өңдеп, жазамыз.",
        "uz": "Ma'lumotlarni o'chirish so'rovi qabul qilindi. 15 ish kuni ichida bajaramiz.",
    },
    "trial_bring": {
        "ru": "Возьмите с собой тетрадь и ручку.",
        "kz": "Дәптер мен қалам алып келіңіз.",
        "uz": "Daftar va ruchka olib keling.",
    },
}

__all__ = ["DEFAULTS"]
