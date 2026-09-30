import { AccountAvatar } from "./AccountAvatar";
import "./blackbird-media-network.css";

export function BlackbirdMediaNetwork({ name, avatarUrl, onBack }: {
  name: string; avatarUrl?: string | null; onBack: () => void;
}) {
  return <section className="bb-media" aria-label="Медиасеть — предварительный просмотр">
    <header className="bb-media-top"><button type="button" onClick={onBack}>← На главную</button><span>BLACKBIRD / МЕДИАСЕТЬ</span><i>ПРОЕКТИРУЕТСЯ</i></header>
    <main className="bb-media-content">
      <div className="bb-media-intro"><small>НОВОЕ ПРОСТРАНСТВО ТОВАРИЩЕСТВА</small><h1>Медиасеть<span>.</span></h1><p>Будущее место для публичных профилей, людей и событий экосистемы. Сейчас это предварительный просмотр — ничего из вашего аккаунта не опубликовано.</p></div>
      <div className="bb-media-layout"><article className="bb-media-profile"><div className="bb-media-profile-cover"><span>BLACKBIRD / IDENTITY</span><b aria-hidden="true">✦</b></div><div className="bb-media-profile-body"><AccountAvatar url={avatarUrl} name={name}/><span className="bb-media-profile-state">ТОЛЬКО ВАШ ПРЕДПРОСМОТР</span><h2>{name}</h2><p>Здесь появится ваш публичный профиль Товарищества — только после отдельного подтверждения публикации.</p><div className="bb-media-profile-fields"><span>О себе <b>Не заполнено</b></span><span>Персонажи <b>Скрыты</b></span><span>Публикации <b>Недоступны</b></span></div></div></article>
      <aside className="bb-media-roadmap"><small>ПРОСТРАНСТВО В РАБОТЕ</small><h2>Не просто лента.<br/>Ваш цифровой след.</h2><div><strong>01 / Публичный профиль</strong><p>Имя, история, проверенные связи и управляемая видимость.</p></div><div><strong>02 / Публикации</strong><p>Заметки и события с источниками из контуров — без раскрытия закрытых данных.</p></div><div><strong>03 / Люди</strong><p>Поиск, подписки и переход к личному разговору в Communicate.</p></div><footer>Публикация и поиск сейчас отключены. Настройки приватности появятся до запуска.</footer></aside></div>
    </main>
  </section>;
}
