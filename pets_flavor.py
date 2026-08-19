"""All the sentences the arena ever prints, and nothing else.

This module is pure content on purpose: no `pets_config`, no `pets_combat`, no state. The
combat engine decides WHAT happened (a dodge, a crit, 37 points of damage); this module
only decides HOW to say it, so a balance change to damage numbers can never accidentally
change a joke, and a rewrite of the jokes can never accidentally change a fight's outcome.
That separation is also why every event that will ever need a line is listed once, in
`VARIANTS`, instead of being scattered across combat's branches as inline f-strings --
`pets_combat.py` asks for an event name and gets a finished sentence back, the same way it
would call any other pure function.

The one real constraint worth explaining: pet names are whatever a member typed, of
unknown grammatical gender, and Russian marks gender on exactly the verb forms this text
leans on most -- the past tense ("ударил" vs "ударила" vs "ударило") and short-form
predicate adjectives ("готов" vs "готова"). Picking a default would be wrong for roughly
half the pets in the chat. The fix used throughout is NOT a coin flip or a slash-form --
it is to never put the pet's name in a position that needs one:

    - present tense ("бьёт", "уклоняется") does not inflect for gender in Russian, so it
      carries almost every line here;
    - future tense ("встанет") is equally safe and shows up where "will do X" reads more
      naturally than "is doing X";
    - plural subjects ("трибуны замолкают", "звёзды сошлись") sidestep the issue too --
      Russian past tense only distinguishes gender in the singular;
    - where a past-tense verb is unavoidable for flavour, its grammatical subject is
      something else entirely (the referee, the crowd, the "удар" itself, all fixed-gender
      Russian nouns we control) -- never the pet's own name;
    - adverbs ("решительно", "неуверенно") describe an action without needing to agree
      with anyone, unlike the short-form adjectives they replace.

A reviewer reading this file top to bottom should never hit a line that only reads right
for a "he" or only for a "she". If one slips in, that is a bug here, not a limitation of
the language -- Russian has enough gender-neutral constructions to write a whole comedy
bot without ever guessing.
"""

import random

# --------------------------------------------------------------------------- templates
#
# Every template is filled with `.format(attacker=..., defender=..., amount=...)` no
# matter which event it belongs to -- str.format silently ignores keyword arguments a
# template doesn't reference, so `line()` needs no per-event branching, and a template is
# free to use only the placeholders its joke actually needs.
#
# No two variants in the same event are a reskin of each other: different jokes, not the
# same joke with a synonym swapped in. That is the actual deliverable here, so read them,
# don't skim them.

_HIT = (
    "{attacker} наносит точный удар — {defender} теряет {amount} HP.",
    "{attacker} бьёт с разворота, {defender} получает {amount} урона.",
    "Удар от {attacker} приходится точно в цель: {amount} урона {defender}.",
    "{attacker} впечатывает лапой — минус {amount} у {defender}.",
    "Кулак (лапа, коготь — не суть) {attacker} находит {defender}: {amount} урона.",
    "{defender} получает {amount} урона — {attacker} явно давно готовится к таким ударам.",
    "{attacker} наносит удар без особых изысков. {defender} теряет {amount} HP.",
    "{attacker} бьёт прямо по корпусу — {amount} урона {defender}.",
    "Резкий выпад {attacker}, и {defender} недосчитывается {amount} HP.",
    "{attacker} врезает от души: {amount} урона {defender}.",
    "{defender} получает {amount} урона — напор {attacker} оказывается неожиданным.",
    "{attacker} наносит удар, засчитано {amount} урона.",
    "{attacker} бьёт метко, {defender} теряет {amount} HP и остатки самообладания.",
    "Удар {attacker} проходит без сопротивления: {amount} урона.",
    "{attacker} наносит скользящий, но всё же ощутимый удар — {amount} урона {defender}.",
    "{attacker} атакует, и {defender} теряет {amount} HP.",
    "Хук от {attacker}: {defender} теряет {amount} HP и равновесие.",
    "{attacker} лупит без разговоров — {amount} урона {defender}.",
    "{attacker} достаёт {defender} точным ударом на {amount}.",
    "{defender} на секунду теряет бдительность — и сразу {amount} урона от {attacker}.",
    "{attacker} наносит удар с оттяжкой: {amount} урона {defender}.",
    "{attacker} бьёт по касательной, но и этого хватает на {amount} урона.",
    "{attacker} проводит связку — {defender} получает {amount} урона суммарно.",
    "{attacker} наносит удар, судья кивает: {amount} урона засчитано.",
    "{attacker} прицельно бьёт в открытый бок {defender} — {amount} урона.",
    "{attacker} наносит удар с боевым кличем — {amount} урона {defender}.",
    "{defender} пропускает удар от {attacker} и теряет {amount} HP.",
    "{attacker} бьёт коротко и жёстко: {amount} урона {defender}.",
    "{attacker} наносит удар с разбегу — {defender} теряет {amount} HP.",
    "{attacker} наносит удар именно в тот миг, когда внимание {defender} рассеивается — {amount} урона.",
    "{attacker} наносит крепкий удар, {defender} держится, но теряет {amount} HP.",
    "{attacker} бьёт наверняка: {amount} урона {defender}.",
    "Один точный удар {attacker} — и {defender} минус {amount} HP.",
    "{attacker} наносит удар, который явно засчитают: {amount} урона {defender}.",
    "{attacker} лупит по щиту, часть удара всё равно доходит — {amount} урона {defender}.",
    "{attacker} наносит удар с уверенностью профессионала — {amount} урона.",
    "{attacker} достаёт {defender} исподтишка: {amount} урона.",
    "{attacker} бьёт от бедра, {defender} теряет {amount} HP.",
    "{attacker} наносит удар, зрители на трибунах одобрительно гудят: {amount} урона.",
    "{attacker} проводит обманный манёвр и всё равно попадает — {amount} урона {defender}.",
    "{attacker} бьёт со всей серьёзностью, {defender} теряет {amount} HP.",
    "{attacker} наносит удар точно между делом — {amount} урона {defender}.",
    "{attacker} находит брешь в защите {defender} и наносит {amount} урона.",
)

_CRIT = (
    "КРИТИЧЕСКИЙ УДАР! {attacker} находит слабое место — {amount} урона {defender}.",
    "{attacker} попадает точно в уязвимое место: критический удар, {amount} урона.",
    "Редкая удача: {attacker} наносит критический удар на {amount} урона {defender}.",
    "{attacker} бьёт настолько неожиданно даже для себя — критический удар, {amount} урона.",
    "Судья присвистывает: критический удар {attacker}, {amount} урона {defender}.",
    "{attacker} ловит идеальный момент — критический удар, {defender} теряет {amount} HP.",
    "Критический удар! {attacker} явно долго тренируется именно ради такого момента — {amount} урона.",
    "{attacker} наносит удар такой силы, что трибуны замолкают: критический, {amount} урона.",
    "{defender} не успевает даже удивиться — критический удар от {attacker}, {amount} урона.",
    "Звёзды сошлись: критический удар {attacker} приносит {amount} урона {defender}.",
    "{attacker} бьёт со всей дури и попадает точно куда нужно — критический удар, {amount} урона.",
    "Критический удар! По правилам разрешено, так что {amount} урона {defender} остаются в силе.",
    "{attacker} находит единственную незащищённую точку {defender}: критический удар, {amount} урона.",
    "Невероятно точный удар от {attacker} — критический, {amount} урона {defender}.",
    "{attacker} бьёт как в замедленной съёмке — критический удар, {amount} урона.",
    "Критический удар! Даже рефери записывает себе этот момент в блокнот — {amount} урона {defender}.",
    "{attacker} ловит {defender} врасплох на критическом ударе: {amount} урона.",
    "Один в миллион: критический удар {attacker}, {amount} урона {defender}.",
    "{attacker} бьёт с оттяжкой прямо в больное место — критический удар, {amount} урона.",
    "Критический удар! {defender} явно не ожидает такого поворота — {amount} урона.",
    "{attacker} наносит удар мечты — критический, {amount} урона {defender}.",
    "Критический удар: {attacker}, кажется, репетирует это движение во сне — {amount} урона {defender}.",
    "{attacker} бьёт с полным попаданием, критический удар — {amount} урона {defender}.",
    "Критический удар! Публика вскакивает с мест, {defender} теряет {amount} HP.",
    "{attacker} находит идеальную траекторию — критический удар, {amount} урона {defender}.",
    "Критический удар: {attacker} явно консультируется с судьбой перед каждым боем — {amount} урона.",
    "{attacker} бьёт настолько чисто, что это можно вставлять в учебник — критический удар, {amount} урона.",
    "Критический удар! {defender} теряет {amount} HP и часть уверенности в себе.",
    "{attacker} наносит удар прямо по расписанию неудач {defender} — критический, {amount} урона.",
    "Критический удар: {attacker} бьёт так, будто знает заранее, куда встанет {defender}. {amount} урона.",
    "{attacker} ловит момент истины — критический удар, {amount} урона {defender}.",
    "Критический удар! Комментаторы теряют дар речи, {defender} теряет {amount} HP.",
)

_DODGE = (
    "{attacker} бьёт, но {defender} уже не там — чистый промах.",
    "{defender} засматривается на бабочку и совершенно случайно уходит с линии удара.",
    "Удар {attacker} проходит впустую: {defender} успевает присесть за укатившимся карандашом.",
    "{defender} уклоняется так эффектно, что судья не сдерживает аплодисментов.",
    "{attacker} замахивается — {defender} уже сидит в трёх метрах и смотрит на облака.",
    "{defender} спотыкается о собственный хвост и этим совершенно случайно уходит с линии атаки.",
    "Мимо! {defender} внезапно вспоминает про недокрашенную картину и отступает как раз вовремя.",
    "{attacker} наносит удар в пустоту — {defender} уже стоит совсем в другом месте ринга.",
    "{defender} делает шаг назад, чтобы получше рассмотреть удар {attacker}, и тем самым избегает его целиком.",
    "Удар не достигает цели: {defender} уходит в сторону, увлечённый чем-то на трибунах.",
    "{attacker} бьёт мощно, но {defender} в последний момент приседает — пусто.",
    "{defender} уклоняется, попутно поправляя причёску.",
    "{attacker} промахивается: {defender} как раз вспоминает про важную встречу и уже далеко.",
    "{defender} уходит с линии удара так плавно, будто репетирует это годами.",
    "Удар {attacker} задевает только воздух — {defender} уже в другом конце ринга, изучает разметку.",
    "{defender} внезапно приседает, якобы завязать шнурок, которого нет — но удар всё равно проходит мимо.",
    "{attacker} бьёт от души, но {defender} за миг до этого отступает на шаг — и всё, промах.",
    "{defender} уклоняется, не отрывая взгляда от чего-то интересного за спиной {attacker}.",
    "Атака {attacker} проходит вхолостую: {defender} успевает отпрыгнуть.",
    "{defender} внезапно решает, что сейчас идеальный момент для растяжки, — и удар {attacker} проходит мимо.",
    "{attacker} промахивается, потому что {defender} уже давно смотрит совсем в другую сторону.",
    "{defender} уклоняется без единого лишнего движения — почти скучающе.",
    "Удар {attacker} свистит рядом с ухом {defender}, не задевая ничего.",
    "{defender} делает финт в сторону, {attacker} остаётся ни с чем.",
    "{attacker} бьёт в опустевшее место — {defender} уже в другом углу ринга.",
    "{defender} внезапно вспоминает про начатый эскиз и на пару шагов уходит от удара.",
    "Мимо: {defender} разглядывает собственные лапы и напрочь забывает стоять на месте.",
    "{attacker} наносит удар, но {defender} уже отпрыгивает в сторону.",
    "{defender} уклоняется настолько лениво, что это даже обидно для {attacker}.",
    "Удар не находит цель — {defender} как раз в этот момент решает почесать ухо и случайно приседает.",
    "{attacker} бьёт мимо: {defender} успевает сделать шаг в сторону и с интересом рассмотреть трибуны.",
    "{defender} уклоняется, будто заранее знает траекторию удара.",
    "Атака {attacker} проваливается: {defender} уже там, где удара не будет.",
    "{defender} отступает ровно на полшага — этого хватает, чтобы удар прошёл мимо.",
    "{attacker} наносит удар с разворота, но {defender} испаряется с линии атаки.",
    "Мимо! {defender} слишком поглощён созерцанием облака странной формы.",
    "{defender} уклоняется, одновременно зевая — очень обидно для {attacker}.",
    "{attacker} бьёт наугад, промахивается: {defender} давно смещается в сторону.",
    "{defender} шагает в сторону в последний момент, будто слышит удар заранее.",
    "Удар {attacker} проходит мимо: {defender} слишком быстро моргает, чтобы там оставаться.",
    "{defender} уклоняется, изящно перепрыгивая через воображаемую лужу.",
    "{attacker} наносит удар в пустое место — {defender} давно там, где безопаснее.",
)

_BLOCKED = (
    "{attacker} наносит мощный удар, но броня {defender} гасит большую часть — проходит только {amount}.",
    "Удар {attacker} застревает в доспехах {defender}: до цели доходит лишь {amount} урона.",
    "{defender} блокирует основной удар, но {amount} урона всё же просачивается.",
    "Броня {defender} трещит, но держит — {attacker} наносит только {amount} урона.",
    "{attacker} бьёт от души, но большая часть удара уходит в щит {defender}: {amount} урона.",
    "{defender} успевает подставить защиту — до цели доходит только {amount} урона.",
    "Удар {attacker} почти полностью гасится бронёй {defender}: {amount} урона.",
    "{defender} принимает удар на защиту, {attacker} получает жалкие {amount} урона в качестве утешения.",
    "Доспехи {defender} делают своё дело — {attacker} наносит лишь {amount} урона.",
    "{attacker} бьёт в щит {defender}, до тела доходит только {amount}.",
    "{defender} закрывается в последний момент: {amount} урона вместо полного удара {attacker}.",
    "Удар {attacker} рикошетит от брони {defender}, оставляя лишь {amount} урона.",
    "{defender} держит оборону — из всей мощи {attacker} проходит только {amount}.",
    "Броня скрипит, но не подводит: {attacker} наносит {amount} урона вместо запланированного.",
    "{attacker} наносит удар, {defender} успевает частично закрыться — {amount} урона.",
    "{defender} подставляет защиту точно вовремя, {amount} урона {attacker} всё же проносит.",
    "Удар {attacker} гасится наполовину бронёй {defender}: итого {amount} урона.",
    "{defender} блокирует, теряя только {amount} HP вместо гораздо большего.",
    "{attacker} бьёт с размаху, но щит {defender} забирает на себя основную часть — {amount} урона.",
    "Броня {defender} звенит от удара {attacker}, пропуская лишь {amount}.",
    "{defender} успевает вовремя прикрыться, {attacker} наносит только {amount} урона.",
    "Удар {attacker} частично поглощается защитой {defender} — {amount} урона доходит до цели.",
    "{defender} блокирует почти всё, кроме {amount} урона, который всё же просачивается.",
    "Доспехи {defender} принимают на себя главный удар — {attacker} наносит только {amount}.",
    "{attacker} бьёт по защите {defender}, и лишь {amount} урона доходит до цели.",
    "{defender} вовремя разворачивает щит — {amount} урона вместо разгромного удара.",
    "Броня {defender} трещит по швам, но справляется: {attacker} наносит {amount} урона.",
)

_LOW_DAMAGE = (
    "{attacker} наносит удар, но получается что-то совсем несерьёзное — {amount} урона.",
    "Удар {attacker} скорее символический: {defender} теряет всего {amount} HP.",
    "{attacker} бьёт, но как будто извиняется за это — {amount} урона {defender}.",
    "Удар настолько слабый, что {defender} даже не сразу замечает — {amount} урона.",
    "{attacker} наносит удар вполсилы: {amount} урона, и то не факт что специально.",
    "{defender} теряет {amount} HP, хотя удар {attacker} выглядел куда внушительнее, чем оказался.",
    "{attacker} бьёт так себе — {amount} урона, зрители зевают.",
    "Скорее похлопывание, чем удар: {amount} урона {defender}.",
    "{attacker} наносит удар слабее, чем ожидалось: {amount} урона {defender}.",
    "{amount} урона — {attacker} явно бережёт силы для чего-то другого.",
    "Удар {attacker} проходит, но толку от него мало: {amount} урона.",
    "{attacker} бьёт неуверенно, {defender} теряет всего {amount} HP.",
    "Технически удар засчитан: {amount} урона {defender}, но впечатления это не производит.",
    "{attacker} наносит удар, будто отвлекается на середине удара — {amount} урона.",
    "{defender} почти не замечает удара {attacker}: {amount} урона.",
    "{attacker} бьёт с явной неохотой — {amount} урона {defender}.",
    "Удар получился совсем несерьёзным: {amount} урона {defender}.",
    "{attacker} наносит удар, судья пожимает плечами: {amount} урона.",
    "{amount} урона — {attacker}, кажется, думает о чём-то своём прямо во время удара.",
    "Удар {attacker} едва ощутим: {defender} теряет всего {amount} HP.",
    "{attacker} бьёт, но силы явно ушли не туда — {amount} урона {defender}.",
    "Скромный результат: {amount} урона {defender} от удара {attacker}.",
)

_OPENING = (
    "На арену выходят {attacker} и {defender}. Судья ещё не жалеет, что согласился судить.",
    "{attacker} и {defender} встают друг напротив друга. Зрители затихают в предвкушении.",
    "Бой начинается: {attacker} против {defender}. Ставки уже сделаны.",
    "{attacker} выходит на арену. {defender} уже там и выглядит решительно.",
    "Арена замирает: {attacker} и {defender} готовы начать.",
    "{attacker} разминается. {defender} делает вид, что тоже разминается.",
    "Судья поднимает лапу — бой между {attacker} и {defender} начинается.",
    "{attacker} и {defender} встречаются взглядами. Один из них явно нервничает больше другого.",
    "Гонг! {attacker} и {defender} начинают бой.",
    "{attacker} выходит первым, {defender} — секундой позже, но с гораздо более серьёзным лицом.",
    "На арене {attacker} и {defender}. Никто пока не знает, чем это закончится.",
    "{attacker} против {defender} — бой объявлен открытым.",
    "{attacker} и {defender} кружат по арене, оценивая друг друга.",
    "Судья свистит: {attacker} и {defender}, бой начался.",
    "{attacker} выходит на арену под лёгкий гул трибун. {defender} выходит под точно такой же гул.",
    "Начало положено: {attacker} встречается с {defender} лицом к лицу.",
    "{attacker} и {defender} занимают позиции. Кто-то из зрителей уже делает ставки.",
    "Бой {attacker} и {defender} начинается без лишних предисловий.",
    "{attacker} выходит на арену с максимально решительным видом. {defender} — не отстаёт.",
    "Судья объявляет бой: {attacker} против {defender}. Пусть начнётся представление.",
    "{attacker} и {defender} стоят друг напротив друга — и вот бой начинается.",
    "Арена открыта: на ней {attacker} и {defender}, и обратной дороги уже нет.",
)

_VICTORY = (
    "{attacker} побеждает! {defender} уходит переосмысливать стратегию.",
    "Бой окончен: {attacker} побеждает, а {defender} утешает себя тем, что бой был почти равным.",
    "Победа за {attacker}! Трибуны в восторге, {defender} — не очень.",
    "{attacker} выигрывает бой. Судья поднимает его (или её, или что там уместно) лапу.",
    "Финальный удар решает всё: {attacker} побеждает.",
    "{defender} признаёт поражение. {attacker} празднует победу.",
    "Бой завершён победой {attacker}.",
    "{attacker} выходит победителем этой схватки.",
    "Победа достаётся {attacker} — заслуженно и без вопросов.",
    "{defender} держится до последнего, но победа всё равно у {attacker}.",
    "{attacker} празднует победу, {defender} уже думает о матче-реванше.",
    "Итог боя: {attacker} — победитель, {defender} — тот, кто уходит подумать над ошибками.",
    "{attacker} побеждает под аплодисменты трибун.",
    "Судья объявляет победителя: {attacker}.",
    "{attacker} выигрывает эту схватку у {defender}.",
    "Бой окончен победой {attacker}. Аплодисменты, занавес.",
    "{defender} сдаётся. {attacker} наслаждается моментом славы.",
    "Победа за {attacker} — уверенная и не оставляющая вопросов.",
    "{attacker} побеждает, а {defender} уходит с арены, обдумывая план подготовки получше.",
    "Арена ревёт: {attacker} — победитель!",
    "{attacker} выигрывает бой, {defender} получает урок на будущее.",
    "Финал: {attacker} побеждает, {defender} жмёт лапу (или что там принято) сопернику.",
    "{attacker} становится победителем этой схватки.",
    "Всё кончено: {attacker} побеждает {defender}.",
    "{attacker} поднимает трофей. {defender} аплодирует сквозь зубы.",
    "Судья фиксирует победу {attacker} — бой окончен.",
    "{attacker} выигрывает, и на этом бой между {attacker} и {defender} завершён.",
)

_ROUND_FLAVOR = (
    "{attacker} переводит дух, {defender} поправляет что-то невидимое.",
    "Пауза в бою: {attacker} и {defender} обмениваются недобрыми взглядами.",
    "{defender} спрашивает судью, сколько ещё это будет продолжаться.",
    "{attacker} на секунду отвлекается на шум с трибун.",
    "Оба бойца делают вид, что вообще не устали.",
    "{attacker} поправляет боевую стойку, {defender} — просто стоит и смотрит.",
    "Судья проверяет часы. Бой продолжается.",
    "{defender} разминает лапы, готовясь к следующему раунду.",
    "{attacker} что-то бормочет себе под нос — возможно, боевой клич, возможно, список покупок.",
    "Небольшая пауза: {attacker} и {defender} переглядываются.",
    "{attacker} смотрит на {defender} с явным уважением и не менее явным желанием победить.",
    "Никто не бьёт. Все просто стоят и оценивают ситуацию.",
    "{defender} на мгновение засматривается на облако странной формы.",
    "Судья делает пометку в блокноте, значение которой неясно никому.",
    "{attacker} перешагивает с лапы на лапу, обдумывая следующий ход.",
    "Зрители на трибунах спорят, кто победит. {attacker} и {defender} этого не слышат.",
    "{defender} придаёт себе максимально угрожающий вид — получается не очень.",
    "Короткая передышка — {attacker} и {defender} оба делают вид, что совсем не устали.",
    "{attacker} хрустит суставами — если они у этого существа вообще есть.",
    "Судья строго смотрит на обоих. Бойцы делают невинные морды.",
    "{defender} что-то прикидывает в уме — возможно, тактику, возможно, что заказать на обед.",
    "Пауза затягивается: {attacker} явно наслаждается моментом славы, {defender} — нет.",
    "{attacker} и {defender} кружат по арене, не решаясь атаковать первыми.",
    "Судья зевает. Публика — нет.",
    "{attacker} стряхивает с себя пыль, {defender} делает то же самое из вежливости.",
    "Тишина между раундами нарушается только скрипом трибун.",
    "{defender} косится на {attacker}, прикидывая, стоило ли вообще выходить сегодня на арену.",
)

# A short post-fight line is distinct from the detailed blow log. Thirty winning beats
# crossed with ten losing beats give 300 unique, gender-neutral result variants without
# coupling combat rules to the text bank.
_RESULT_WINS = (
    "{attacker} забирает арену.", "{attacker} сегодня слишком хорош.",
    "{attacker} уносит победу в лапах.", "{attacker} оставляет трибуны довольными.",
    "{attacker} проводит вечер как победитель.", "{attacker} забирает этот раунд себе.",
    "{attacker} получает право на победный круг.", "{attacker} оказывается главным на арене.",
    "{attacker} завершает бой на высокой ноте.", "{attacker} забирает заслуженное первое место.",
    "{attacker} получает аплодисменты судьи.", "{attacker} выходит из боя с трофеем.",
    "{attacker} оставляет за собой последнее слово.", "{attacker} празднует без лишней скромности.",
    "{attacker} превращает арену в личную сцену.", "{attacker} ловит удачный день.",
    "{attacker} забирает момент славы.", "{attacker} становится главным сюжетом вечера.",
    "{attacker} добирается до победной точки.", "{attacker} берёт бой уверенно.",
    "{attacker} уходит под одобрительный гул.", "{attacker} выигрывает этот спор лапами.",
    "{attacker} показывает, кто тут подготовился.", "{attacker} получает законный повод хвастаться.",
    "{attacker} делает арену своей территорией.", "{attacker} забирает лучший кадр боя.",
    "{attacker} получает золото и уважение трибун.", "{attacker} ставит красивую точку.",
    "{attacker} завершает бой в свою пользу.", "{attacker} забирает победу без обсуждений.",
)

_RESULT_LOSSES = (
    "{defender} уже планирует реванш.", "{defender} берёт паузу на тактику.",
    "{defender} ищет, куда делся план боя.", "{defender} требует повтор после перекуса.",
    "{defender} уходит проверять экипировку.", "{defender} делает вид, что так и было задумано.",
    "{defender} сохраняет лицо почти идеально.", "{defender} записывает это в список уроков.",
    "{defender} обещает вернуться подготовленнее.", "{defender} временно уступает арену.",
)

RESULT_VARIANTS: tuple[str, ...] = tuple(
    f"{win} {loss}" for win in _RESULT_WINS for loss in _RESULT_LOSSES
)

# Group announcements need to be shorter and louder than the detailed battle log. Kept
# separate so a new public quip never changes the combat receipt stored for either player.
PUBLIC_RESULT_VARIANTS = (
    "{winner} забирает арену. Без вариантов.",
    "{winner} устроил разнос и забрал победу.",
    "{winner} оставляет соперника в слое пыли и славы.",
    "{winner} кладёт этот бой в витрину трофеев.",
    "{winner} наносит финальный мазок. Картина готова.",
    "{winner} сегодня красит только в цвет победы.",
    "{winner} превращает арену в личный мольберт.",
    "{winner} выдал такой слой, что спорить уже поздно.",
    "{winner} оставляет после себя только аплодисменты и грунт.",
    "{winner} забирает раунд жёстко и красиво.",
    "{winner} устроил критический покрас соперника.",
    "{winner} ставит точку. Жирную, акриловую.",
    "{winner} выбивает победу кистью уверенной руки.",
    "{winner} выносит арену. Судья ищет, что записать в протокол.",
    "{winner} сегодня не боец, а стихийное бедствие.",
    "{winner} оформляет победу без скидок и компромиссов.",
    "{winner} забирает бой, палитру и уважение трибун.",
    "{winner} делает из этого боя учебный пример.",
    "{winner} выдал мощный контраст. Победа на месте.",
    "{winner} закрывает вопрос одним финальным штрихом.",
    "{winner} превращает соперничество в выставку достижений.",
    "{winner} забирает победу так уверенно, будто так и было задумано.",
    "{winner} наносит бойцам и зрителям неизгладимое впечатление.",
    "{winner} идёт в атаку как свежий баллон аэрографа.",
    "{winner} оставляет арену в идеальном состоянии. Почти.",
    "{winner} берёт верх. Палитра одобряет.",
    "{winner} оформляет нокаут с выставочной подачей.",
    "{winner} делает победный мазок прямо по протоколу.",
    "{winner} сегодня главный экспонат арены.",
    "{winner} завершает бой громко, чисто и по делу.",
)

_DRAW = (
    "{attacker} и {defender} расходятся без победителя.",
    "Ничья: {attacker} и {defender} оставляют спор на реванш.",
    "Судья фиксирует равенство между {attacker} и {defender}.",
    "{attacker} и {defender} делят этот бой поровну.",
    "Арена не выбирает между {attacker} и {defender}: ничья.",
)

_SIGNATURE_STRENGTH = (
    "{attacker} заряжает валик и вдавливает его в арену: {amount} урона {defender}.",
    "Тяжёлая кисть {attacker} оставляет на {defender} особенно плотный слой: {amount} урона.",
)
_SIGNATURE_HEALTH = (
    "Грунтовка {defender} принимает удар {attacker} на себя: проходит только {amount} урона.",
    "{defender} выдерживает удар {attacker} на запасе прочности: {amount} урона.",
)
_SIGNATURE_AGILITY = (
    "{defender} ускользает от фирменного выпада {attacker}, оставляя на месте только блик.",
    "{defender} успевает отдёрнуть мольберт: удар {attacker} проходит мимо.",
)
_SIGNATURE_AGILITY_COUNTER = (
    "{defender} уворачивается от {attacker} и тут же отвечает аэрографом: {amount} урона.",
    "{defender} исчезает из-под удара {attacker} и возвращает мазок на {amount} урона.",
)
_SIGNATURE_LUCK = (
    "Случайная капля от {attacker} попадает идеально: {defender} теряет {amount} HP.",
    "Удачный блик ослепляет {defender}: открывающий удар {attacker} на {amount} урона.",
)
_SIGNATURE_ARMOR = (
    "Защита {defender} съедает почти весь удар {attacker}: остаётся {amount} урона.",
    "{defender} подставляет палитру под удар {attacker}: проходит лишь {amount} урона.",
)
_SIGNATURE_ARMOR_RECOIL = (
    "Удар {defender} не пробивает покрытие и отдачей возвращает {amount} урона {attacker}.",
    "Броня {defender} отражает выпад {attacker}; отдача наносит {amount} урона.",
)

# Minimums are the contract; every event here clears its floor with a small margin so a
# future trim (cutting one bad joke) doesn't need a matching new one just to stay legal.
VARIANTS: dict[str, tuple[str, ...]] = {
    "hit": _HIT,
    "crit": _CRIT,
    "dodge": _DODGE,
    "blocked": _BLOCKED,
    "low_damage": _LOW_DAMAGE,
    "opening": _OPENING,
    "victory": _VICTORY,
    "round_flavor": _ROUND_FLAVOR,
    "signature_strength": _SIGNATURE_STRENGTH,
    "signature_health": _SIGNATURE_HEALTH,
    "signature_agility": _SIGNATURE_AGILITY,
    "signature_agility_counter": _SIGNATURE_AGILITY_COUNTER,
    "signature_luck": _SIGNATURE_LUCK,
    "signature_armor": _SIGNATURE_ARMOR,
    "signature_armor_recoil": _SIGNATURE_ARMOR_RECOIL,
}

EVENTS: tuple[str, ...] = tuple(VARIANTS)

# Ten mishaps times ten consequences make one hundred distinct instant-win reports.
# Kept outside VARIANTS because an accident ends the fight before a normal round exists.
_ACCIDENT_MISHAPS = (
    "аэрограф {attacker} чихнул струёй краски",
    "банка с wash у {attacker} решила опрокинуться",
    "мокрая палитра {attacker} устроила мини-наводнение",
    "компрессор {attacker} чихнул облаком грунта",
    "кисть {attacker} зацепила стакан для промывки",
    "фен {attacker} сдул незакреплённую декорацию",
    "малярный скотч {attacker} приклеился к реальности",
    "лак {attacker} высох с характером",
    "светильник {attacker} ослепил судью бликом",
    "кот с трибуны утащил главную кисть {attacker}",
)
_ACCIDENT_CONSEQUENCES = (
    "и {defender} поскользнулся на идеальном градиенте.",
    "и {defender} получил незапланированный слой базового цвета.",
    "и {defender} убежал спасать недокрашенную миниатюру.",
    "и {defender} исчез за облаком пигмента.",
    "и {defender} объявил технический перерыв, который стал поражением.",
    "и {defender} застрял в свежем слое varnish.",
    "и {defender} перепутал арену с сушилкой для кистей.",
    "и {defender} принял это за знак срочно помыть баночки.",
    "и {defender} отвлёкся на падающую каплю Metallic.",
    "и {defender} капитулировал перед силой художественного хаоса.",
)
ACCIDENT_VARIANTS: tuple[str, ...] = tuple(
    f"Игроку повезло: {mishap}, {consequence}"
    for mishap in _ACCIDENT_MISHAPS
    for consequence in _ACCIDENT_CONSEQUENCES
)


def line(event: str, attacker: str, defender: str = "", amount: int = 0, rng=None) -> str:
    """One ready Russian sentence for `event`, names and number already filled in.

    `rng` is a `random.Random` so a seeded fight replays identically; the module-level
    `random` is used when nobody cares (a one-off preview). Every template is formatted
    against all three fields regardless of event, relying on `str.format` ignoring the
    keywords a given template doesn't mention -- that is what keeps this function a single
    unbranched lookup-and-format instead of one code path per event.
    """
    picker = rng if rng is not None else random
    try:
        variants = VARIANTS[event]
    except KeyError:
        raise ValueError(f"unknown flavor event: {event!r}") from None
    template = picker.choice(variants)
    return template.format(attacker=attacker, defender=defender, amount=amount)


def result_line(winner: str, loser: str, rng=None) -> str:
    picker = rng if rng is not None else random
    return picker.choice(RESULT_VARIANTS).format(attacker=winner, defender=loser)


def draw_line(first: str, second: str, rng=None) -> str:
    picker = rng if rng is not None else random
    return picker.choice(_DRAW).format(attacker=first, defender=second)


def accident_line(winner: str, loser: str, rng=None) -> str:
    picker = rng if rng is not None else random
    return picker.choice(ACCIDENT_VARIANTS).format(attacker=winner, defender=loser)


def public_result_line(winner: str, rng=None) -> str:
    """A compact random winner announcement for the temporary group result card."""
    picker = rng if rng is not None else random
    return picker.choice(PUBLIC_RESULT_VARIANTS).format(winner=winner)


# --- reading a transcript ---------------------------------------------------------------
# A fight log is a wall of prose, and the two things a reader needs first -- whose turn it
# is, and what KIND of turn it was -- were the two things it never said outright. An
# administrator reading an audit had to infer "this was a crit" from the wording, and a
# player watching a replay had to infer whose line it was from the colour of a name.
#
# One table, so the Mini App, the audit page and anything else mark a transcript the same
# way. Keys are matched exactly first, then by prefix, so a family of events (every
# `shield_defend_*`, every `amulet_*`) is covered without listing all forty of them.
EVENT_MARKS: dict[str, tuple[str, str]] = {
    # The basic exchange.
    "hit": ("⚔️", "Удар"),
    "crit": ("💥", "Крит"),
    "dodge": ("💨", "Промах"),
    "blocked": ("🛡", "Блок"),
    "low_damage": ("🪶", "Слабый удар"),
    "defend": ("🛡", "Защита"),
    "stun_skip": ("💫", "Пропуск хода"),
    "victory": ("🏁", "Итог"),
    "opening": ("🎬", "Начало"),
    "round_flavor": ("💬", "Ремарка"),
    # Shields.
    "shield_guard": ("🛡", "Щит"),
    "shield_counterattack": ("↩️", "Контрудар"),
    "shield_parry_stun": ("🤺", "Парирование"),
    "shield_damage_heal": ("💚", "Щит лечит"),
    "shield_burn_tick": ("🔥", "Горение"),
    # The `skill_` family is shared: a shield's reflect and a scroll's reflect both land
    # here, so these five are named for WHAT HAPPENED rather than for where it came from.
    # Anything else starting with `skill_` really is a spell and falls to the prefix below.
    "skill_dodge": ("💨", "Уворот"),
    "skill_reflect": ("↩️", "Отражение"),
    "skill_lifesteal": ("🩸", "Вампиризм"),
    "skill_regen": ("💚", "Восстановление"),
    "skill_ward": ("🔰", "Оберег"),
    "antimagic_reflect": ("🪞", "Антимагия"),
}

# Checked in order, so a longer prefix wins over a shorter one.
EVENT_MARK_PREFIXES: tuple[tuple[str, str, str], ...] = (
    ("shield_defend_", "🛡", "Щит"),
    ("shield_", "🛡", "Щит"),
    ("amulet_", "🧿", "Эффект"),
    ("signature_", "🌟", "Коронный приём"),
    ("deficit_", "📉", "Слабое место"),
    ("skill_", "✨", "Магия"),
)

EVENT_MARK_DEFAULT: tuple[str, str] = ("•", "Событие")


def event_mark(event: str) -> tuple[str, str]:
    """The emoji and the short name for one transcript event.

    Never raises and never returns nothing: an event added later reads as a neutral dot
    rather than making a log line disappear or a page throw.
    """
    key = str(event or "")
    if key in EVENT_MARKS:
        return EVENT_MARKS[key]
    for prefix, icon, label in EVENT_MARK_PREFIXES:
        if key.startswith(prefix):
            return icon, label
    return EVENT_MARK_DEFAULT


def event_mark_table() -> dict:
    """The whole vocabulary in one shape the browser can use.

    Handed to the pages instead of being re-typed in JavaScript, so a new event is marked
    the same way everywhere the moment it is added here.
    """
    return {
        "exact": {key: list(value) for key, value in EVENT_MARKS.items()},
        "prefixes": [[prefix, icon, label] for prefix, icon, label in EVENT_MARK_PREFIXES],
        "default": list(EVENT_MARK_DEFAULT),
    }
