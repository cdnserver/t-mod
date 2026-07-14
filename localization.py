import json
import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_LOCALE: dict[str, Any] = {'bot': {'brand_name': 'T-Mod',
         'brand_description': 'Модератор Товарищества',
         'status_text': 'Товарищество - светлый круг'},
 'commands': {'sgl_name': 'sgl',
              'sgl_description': 'Проверить, что бот отвечает',
              'activity_name': 'activity',
              'activity_description': 'Проверить активность роли или пользователя'},
 'sgl': {'response': 'Пинг есть.'},
 'common': {'no_data': 'нет данных',
            'just_now': 'только что',
            'ago_minutes': '{minutes} мин. назад',
            'ago_hours': '{hours} ч. назад',
            'ago_days': '{days} дн. назад',
            'channel_empty': '—',
            'details_empty': '—',
            'list_separator': ', ',
            'unknown_user': 'Неизвестный пользователь',
            'unknown_channel': 'Неизвестный канал',
            'db_id': 'ID записи в БД'},
 'activity': {'guild_only': 'Команда работает только на сервере Discord.',
              'missing_members_intent': 'Не могу получить список участников. Включи Server Members '
                                        'Intent в Discord Developer Portal и перезапусти бота.',
              'menu_title': 'Режим проверки: **{scope}**. Выберите роль, активность которой нужно '
                            'проверить:',
              'menu_placeholder': 'Выберите роль для проверки активности',
              'menu_not_for_you': 'Это меню открыто не для Вас.',
              'role_selected': 'Выбрана роль: **{role_name}**. Режим: **{scope}**. Ниже отправляю '
                               'отчёт.',
              'no_members': 'У роли **{role_name}** нет пользователей или бот их не видит.',
              'header': '**Активность пользователей с ролью {role_name}**\n'
                        'Режим: **{scope}**.\n'
                        'Сортировка: самые неактивные сверху.\n'
                        'Участников в роли: `{member_count}`.\n'
                        'Собирается: {tracked}',
              'footer': '\n'
                        '_Данные считаются только с момента запуска бота и миграции старого '
                        'activity.json._',
              'line_no_data': '• {mention} — **{no_data}** | бот ещё не видел активности в этом '
                              'режиме',
              'line_active': '• {mention} — **{ago}** | {event} | {datetime} | `{channel}` | '
                             '`{details}` | событий: `{total_events}`',
              'user_header': '**Последние действия пользователя {mention}**\n'
                             'Пользователь: `{display_name}` / `{user_id}`\n'
                             'Режим: **{scope}**\n'
                             'Показано событий: `{limit}`',
              'user_no_events': 'У пользователя {mention} пока нет сохранённых действий в режиме '
                                '**{scope}**.',
              'user_event_line': '• **{datetime}** ({ago}) — {event} | `{channel}` | `{details}`',
              'scope': {'all': 'Вся активность',
                        'tvrs': 'Только сервер Товарищества',
                        'sgl': 'Только категория SGL'},
              'options': {'type_description': 'Режим проверки: all, tvrs или sgl',
                          'user_description': 'Показать последние действия конкретного '
                                              'пользователя'}},
 'tracked': {'messages': 'сообщения',
             'message_edits': 'редактирование сообщений',
             'message_deletes': 'удаление сообщений',
             'reactions': 'реакции',
             'voice': 'войс',
             'voice_status': 'статус в войсе',
             'typing': 'typing',
             'member_updates': 'изменения профиля/ролей',
             'member_join_leave': 'вход/выход с сервера',
             'presence': 'presence/status',
             'commands': 'команды бота',
             'nothing': 'ничего'},
 'events': {'message': 'сообщение в чате',
            'message_edit': 'отредактировал сообщение',
            'message_delete': 'удалил сообщение',
            'reaction_add': 'поставил реакцию',
            'reaction_remove': 'убрал реакцию',
            'voice_join': 'зашёл в войс',
            'voice_leave': 'вышел из войса',
            'voice_move': 'перешёл между войсами',
            'voice_status': 'изменил состояние в войсе',
            'typing': 'печатал сообщение',
            'member_update': 'изменение профиля/ролей',
            'member_join': 'зашёл на сервер',
            'member_leave': 'вышел с сервера',
            'presence_online': 'стал онлайн',
            'presence_idle': 'стал неактивен',
            'presence_dnd': 'стал не беспокоить',
            'presence_offline': 'стал офлайн',
            'presence_update': 'изменил статус/активность',
            'command_sgl': 'использовал /sgl',
            'command_activity': 'использовал /activity',
            'command_sg_msg': 'использовал /sg_msg',
            'command_sg_addnews': 'использовал /sg_addnews',
            'command_sg_initiate': 'использовал /sg_initiate',
            'command_sg_newcase': 'использовал /sg_newcase',
            'command_sg_clink': 'использовал /sg_clink',
            'command_sg_closecase': 'использовал /sg_closecase',
            'command_sglaudio': 'использовал /sglaudio',
            'command_sg': 'использовал /sg'},
 'details': {'message': 'message',
             'message_edit': 'edit',
             'message_delete': 'delete',
             'reaction': '{emoji}',
             'typing': 'typing',
             'voice_join': 'join:{channel}',
             'voice_leave': 'leave:{channel}',
             'voice_move': '{before_channel} -> {after_channel}',
             'voice_status_item_mute': 'mute',
             'voice_status_item_deaf': 'deaf',
             'voice_status_item_stream': 'stream',
             'voice_status_item_video': 'video',
             'voice_status_item_suppress': 'suppress',
             'voice_status_item_requested_to_speak': 'requested_to_speak',
             'voice_status': '{items}',
             'member_display_name': 'display_name',
             'member_roles': 'roles',
             'member_update': '{items}',
             'member_join': 'join',
             'member_leave': 'leave',
             'presence_status': '{before_status} -> {after_status}',
             'presence_activity': 'activities',
             'command_sgl': '/sgl',
             'command_activity': '/activity',
             'command_sg_msg': '/sg_msg',
             'command_sg_addnews': '/sg_addnews',
             'command_sg_initiate': '/sg_initiate',
             'command_sg_newcase': '/sg_newcase',
             'command_sg_clink': '/sg_clink',
             'command_sg_closecase': '/sg_closecase',
             'command_sglaudio': '/sglaudio',
             'command_sg': '/sg'},
 'sgbureau': {'module_name': 'SGL Bureau',
              'module_description': 'Юридическое бюро SGL',
              'commands': {'sg_addnews_name': 'sg_addnews',
                           'sg_addnews_description': 'Добавить новость SGL Bureau',
                           'sg_initiate_name': 'sg_initiate',
                           'sg_initiate_description': 'Инициировать каналы SGL Bureau',
                           'sg_initiate_cs_description': 'Номер первого кейса',
                           'sg_newcase_name': 'sg_newcase',
                           'sg_newcase_description': 'Создать новый кейс SGL Bureau',
                           'sg_newcase_client_description': 'Клиент кейса',
                           'sg_newcase_lawyer_description': 'Ведущий адвокат. По умолчанию Saoul '
                                                            'Goodman',
                           'sg_newcase_secretary_description': 'Секретарь кейса. Необязательно',
                           'sg_clink_name': 'sg_clink',
                           'sg_clink_description': 'Добавить ссылку на исковое заявление',
                           'sg_clink_link_description': 'Ссылка на исковое заявление',
                           'sg_closecase_name': 'sg_closecase',
                           'sg_closecase_description': 'Закрыть кейс SGL Bureau',
                           'sg_name': 'sg',
                           'sg_description': 'Панель управления SGL Bureau',
                           'sg_user_description': 'Открыть обзор пользователя как клиента SGL',
                           'sg_case_description': 'Открыть панель кейса по номеру'},
              'errors': {'guild_only': 'Команда работает только на сервере Discord.',
                         'wrong_channel': 'Команду можно использовать только в канале '
                                          '<#{allowed_channel_id}>.',
                         'no_permission': 'Эту команду может использовать только администратор или '
                                          'стафф SGL Bureau.',
                         'target_channel_not_found': 'Не найден канал для публикации новости: '
                                                     '`{target_channel_id}`.',
                         'global_channel_not_found': 'Не найден общий канал новостей Товарищества: '
                                                     '`{target_channel_id}`.',
                         'target_not_text_channel': 'Канал <#{target_channel_id}> не является '
                                                    'текстовым каналом.',
                         'send_failed': 'Не смог отправить новость в канал <#{target_channel_id}>. '
                                        'Ошибка: `{error}`',
                         'db_failed': 'Новость отправлена, но запись в БД не сохранилась. Ошибка: '
                                      '`{error}`',
                         'empty_text': 'Текст новости не может быть пустым.',
                         'empty_news': 'Заголовок и текст новости не могут быть пустыми.'},
              'news': {'note_field_name': 'Пометка',
                       'footer': '{bot_name} • {datetime} • Автор: {author_display} ({author_id})',
                       'success': 'Новость отправлена в <#{target_channel_id}>. ID записи в БД: '
                                  '`{record_id}`',
                       'success_global': 'Новость отправлена в <#{bureau_channel_id}> и '
                                         '<#{global_channel_id}>. ID записи в БД: `{record_id}`'},
              'console': {'sent': 'SGL Bureau news sent by {author} to channel '
                                  '{target_channel_id}. DB id: {record_id}',
                          'reorder_failed': 'SGL Bureau maintenance failed: {error}'},
              'modal': {'title': 'Новая публикация SGL Bureau',
                        'field_title_label': 'Заголовок',
                        'field_title_placeholder': 'Например: Важное объявление SGL',
                        'field_body_label': 'Текст новости',
                        'field_body_placeholder': 'Можно использовать Discord-форматирование: '
                                                  '**жирный**, списки, переносы строк...',
                        'field_note_label': 'Дополнительная пометка',
                        'field_note_placeholder': 'Необязательно. Например: Срочно / Важно / Для '
                                                  'клиентов',
                        'field_image_label': 'Ссылка на изображение',
                        'field_image_placeholder': 'Необязательно. https://...',
                        'field_global_label': 'Дублировать в общие новости?',
                        'field_global_placeholder': 'Напишите да или yes, если нужно дублировать'},
              'initiate': {'intro_title': 'Юридическое бюро SGL',
                           'intro_description': '**SGL** — независимое коллегиальное юридическое '
                                                'объединение, специализирующееся на ведении '
                                                'судебных разбирательств, подготовке исковых '
                                                'заявлений, жалоб в органы прокуратуры и '
                                                'комплексном взаимодействии с государственными '
                                                'структурами.',
                           'contact_field_name': 'Контактные данные',
                           'contact_value': '**Адвокат:** Saoul Goodman\n'
                                            '**Номер тел.:** `8654-9072`\n'
                                            '**Банковский счет:** `215436`\n'
                                            '**Почта:** `lvscm@sgl.gov`',
                           'footer': 'SGL Bureau • правовая поддержка и судебное сопровождение',
                           'costs_title': 'Стоимость услуг SGL',
                           'costs_description': '**Судебный иск** — от `0.5 кк`\n'
                                                '**Судебный иск большой сложности** *(хайки, '
                                                'депки, фамки)* — от `1.5 кк`\n'
                                                '**Жалоба в прокуратуру** — `0.2 кк`, если '
                                                'отсутствует семейная скидка\n'
                                                '**Юридические консультации** — от `0.2 кк`',
                           'recommend_court_title': 'Когда рекомендован суд',
                           'recommend_court_value': 'Если у вас имеется судимость, которую вы '
                                                    'хотите снять, а также если вы хотите добиться '
                                                    'компенсации до `250.000$`.',
                           'recommend_prosecutor_title': 'Когда рекомендована прокуратура',
                           'recommend_prosecutor_value': 'Если ваша цель — добиться проверки в '
                                                         'отношении сотрудника, его увольнения и '
                                                         'привлечения к ответственности.',
                           'other_services_title': 'Остальные услуги',
                           'other_services_value': 'По договоренности.',
                           'channels_missing': 'Не найдены каналы для инициации: `{channels}`.',
                           'success': 'Инициация выполнена. Сообщения отправлены в '
                                      '<#{start_channel_id}> и <#{costs_channel_id}>. Номер '
                                      'первого кейса: `{cs}`. Статус нумерации: `{seed_status}`.',
                           'costs_plain': '# Стоимость услуг SGL\n'
                                          '\n'
                                          '### Основные направления\n'
                                          '**Судебный иск** — от `0.5 кк`\n'
                                          '**Судебный иск большой сложности** *(хайки, депки, '
                                          'фамки)* — от `1.5 кк`\n'
                                          '**Жалоба в прокуратуру** — `0.2 кк`, если отсутствует '
                                          'семейная скидка\n'
                                          '**Юридические консультации** — от `0.2 кк`\n'
                                          '\n'
                                          '### Когда лучше идти в суд\n'
                                          'Рекомендую суд, если у вас имеется судимость, которую '
                                          'вы хотите снять, а также если вы хотите добиться '
                                          'компенсации до `250.000$`.\n'
                                          '\n'
                                          '### Когда лучше обращаться в прокуратуру\n'
                                          'Рекомендую прокуратуру, если ваша цель — добиться '
                                          'проверки в отношении сотрудника, его увольнения и '
                                          'привлечения к ответственности.\n'
                                          '\n'
                                          '### Дополнительно\n'
                                          'Остальные услуги — по договоренности.\n'
                                          '\n'
                                          '---\n'
                                          '**Адвокат:** Saoul Goodman\n'
                                          '**Номер тел.:** `8654-9072`\n'
                                          '**Банковский счет:** `215436`\n'
                                          '**Почта:** `lvscm@sgl.gov`'},
              'case': {'init_params_button': 'Инициировать параметры',
                       'no_secretary': 'секретарь не назначен',
                       'greeting_title': 'Кейс №{case_number} создан',
                       'greeting_description': 'Открыт приватный канал кейса **№{case_number}**.\n'
                                               'Клиент: {client}\n'
                                               'Ведущий адвокат: {lawyer}\n'
                                               'Секретарь: {secretary}',
                       'client_field': 'Клиент',
                       'lawyer_field': 'Ведущий адвокат',
                       'secretary_field': 'Секретарь',
                       'next_step_field': 'Следующий шаг',
                       'next_step_value': 'Нажмите кнопку ниже, чтобы инициировать параметры '
                                          'клиента. Форму может заполнить клиент, ведущий адвокат '
                                          'или секретарь.',
                       'footer': '{bot_name} • SGL Bureau case system',
                       'channel_topic': 'SGL case #{case_number} | Client: {client} | Lawyer: '
                                        '{lawyer}',
                       'audit_newcase_reason': 'SGL Bureau case #{case_number}',
                       'audit_pin_link_reason': 'SGL case #{case_number}: claim link pin',
                       'newcase_success': 'Создан кейс **№{case_number}**: <#{channel_id}>.',
                       'service_select_intro': 'Кейс **№{case_number}**. Выберите тип обращения, '
                                               'затем откроется форма данных клиента.',
                       'service_select_placeholder': 'Выберите тип обращения',
                       'service_court_label': 'Суд',
                       'service_court_description': 'Судебный иск / судебное производство',
                       'service_prosecutor_label': 'Прокуратура',
                       'service_prosecutor_description': 'Жалоба / прокурорская проверка',
                       'service_other_label': 'Другое',
                       'service_other_description': 'Иная юридическая услуга',
                       'service_value': {'court': 'Суд',
                                         'prosecutor': 'Прокуратура',
                                         'other': 'Другое'},
                       'params_modal_title': 'Параметры кейса №{case_number}',
                       'field_nick_label': 'Ник Name Name',
                       'field_nick_placeholder': 'Например: Marcus Delacruz',
                       'field_static_label': 'Статик',
                       'field_static_placeholder': 'Только цифры',
                       'field_bank_label': 'Банковский счет',
                       'field_bank_placeholder': 'Только цифры',
                       'field_phone_label': 'Номер телефона',
                       'field_phone_placeholder': 'Только цифры',
                       'field_passport_label': 'Ссылка на паспорт',
                       'field_passport_placeholder': 'Любая ссылка, кроме Yapix',
                       'params_saved_private': 'Данные клиента сохранены и опубликованы в канал '
                                               'кейса.',
                       'client_data_title': 'Данные клиента по кейсу №{case_number}',
                       'request_type_field': 'Тип обращения',
                       'nickname_field': 'Никнейм',
                       'static_field': 'Статик',
                       'bank_field': 'Банковский счет',
                       'phone_field': 'Телефон',
                       'passport_field': 'Паспорт',
                       'filled_by_footer': 'Заполнил: {actor} • {datetime}',
                       'situation_title': 'Разбор ситуации по кейсу №{case_number}',
                       'situation_description': 'Теперь нужно одним сообщением отправить разбор '
                                                'ситуации. После отправки бот оформит его в '
                                                'аккуратный блок для адвоката и секретаря.',
                       'situation_required_field': 'Что обязательно указать',
                       'situation_required_value': '1. Дата и время инцидента\n'
                                                   '2. Доказательства\n'
                                                   '3. Лицо, которое планируется обвинить '
                                                   '(никнейм)\n'
                                                   '4. Позиция лица, которое планируется обвинить '
                                                   '(место работы, ранг, если имеется)\n'
                                                   '5. Есть ли свидетели\n'
                                                   '6. Желаемый исход суда',
                       'situation_footer': 'Сообщение может отправить клиент, ведущий адвокат или '
                                           'секретарь.',
                       'situation_saved_title': 'Разбор ситуации сохранён • кейс №{case_number}',
                       'situation_saved_footer': 'Отправил: {author} • {datetime}',
                       'link_title': 'Исковое заявление • кейс №{case_number}',
                       'link_description': 'Ссылка на исковое заявление:\n{link}',
                       'link_footer': 'Добавил: {author} • {datetime}',
                       'link_pin_failed': 'Ссылка сохранена, но закрепить сообщение не получилось. '
                                          'Проверьте право Manage Messages у бота.',
                       'close_modal_title': 'Завершение кейса {case_number}',
                       'close_publish_label': 'Портфолио? да/нет',
                       'close_publish_placeholder': 'Напишите да, если публикуем кейс в портфолио',
                       'close_description_label': 'Описание для наблюдателей',
                       'close_description_placeholder': 'Кратко и анонимно опишите суть кейса для '
                                                        'портфолио',
                       'close_saved_private': 'Кейс №{case_number} закрыт.',
                       'closed_title': 'Кейс №{case_number} завершён',
                       'closed_description': 'Кейс завершил: {actor}\n'
                                             '\n'
                                             'Канал будет автоматически перенесён в архив примерно '
                                             'через `{archive_hours}` ч. Клиент сохранит доступ к '
                                             'этому каналу.',
                       'portfolio_title': 'SGL разобрали ещё один кейс • №{case_number}',
                       'portfolio_description': '**Кейс:** `№{case_number}`\n'
                                                '**Исковое заявление:** {link}\n'
                                                '\n'
                                                '**Описание:**\n'
                                                '{description}',
                       'portfolio_footer': 'SGL Bureau portfolio • {datetime} • данные клиента '
                                           'скрыты',
                       'errors': {'not_case_channel': 'Эта команда работает только в канале '
                                                      'конкретного кейса SGL.',
                                  'not_case_participant': 'Вы не являетесь участником этого кейса.',
                                  'case_mismatch': 'Форма открыта не для этого кейса. Откройте '
                                                   'кнопку заново.',
                                  'nick_format': 'Ник должен быть в формате `Name Name`.',
                                  'static_digits': 'Статик должен содержать только цифры.',
                                  'bank_digits': 'Банковский счет должен содержать только цифры.',
                                  'phone_digits': 'Номер телефона должен содержать только цифры.',
                                  'passport_url': 'Паспорт должен быть ссылкой формата '
                                                  '`https://...`.',
                                  'passport_yapix': 'Ссылки Yapix запрещены. Загрузите паспорт на '
                                                    'другой хостинг.',
                                  'default_lawyer_not_found': 'Не найден адвокат по умолчанию: '
                                                              '`{user_id}`. Укажите адвоката '
                                                              'вручную.',
                                  'category_not_found': 'Не найдена категория SGL: '
                                                        '`{category_id}`.',
                                  'channel_create_failed': 'Не смог создать канал кейса. Ошибка: '
                                                           '`{error}`',
                                  'db_case_attach_failed': 'Канал создан, но не получилось '
                                                           'привязать его к записи в БД.',
                                  'link_no_permission': 'Ссылку может добавить ведущий адвокат, '
                                                        'секретарь, администратор или стафф SGL.',
                                  'link_url': 'Ссылка должна начинаться с `http://` или '
                                              '`https://`.',
                                  'close_no_permission': 'Закрыть кейс может только ведущий '
                                                         'адвокат, секретарь или администратор.',
                                  'no_claim_link': 'Перед закрытием с публикацией в портфолио '
                                                   'нужно добавить ссылку через `/sg_clink`.',
                                  'portfolio_not_found': 'Не найден канал портфолио: '
                                                         '`{channel_id}`.'},
                       'audit_rename_reason': 'SGL case {case_number}: status {status}',
                       'audit_archive_reason': 'SGL case {case_number}: move to archive',
                       'archived_message': 'Кейс №{case_number} перенесён в архив. Доступ клиента '
                                           'сохранён.',
                       'archive_failed': 'Не удалось автоматически перенести кейс в архив. '
                                         'Проверьте права Manage Channels у бота.',
                       'archive_category_missing': 'Не найдена архивная категория: '
                                                   '`{category_id}`. Кейс останется в текущей '
                                                   'категории до исправления настроек.'},
              'sg': {'status_closed': '✋ Закрыт',
                     'status_claim_link': '🧘\u200d♂️ Есть ссылка на иск',
                     'status_ready_no_link': '👀 Данные есть, ссылки на иск нет',
                     'status_waiting_data': '⏳ Ожидаются данные',
                     'case_short_line': '`{case_number}` {emoji} **{status}** • {request_type} • '
                                        '{nick}{channel}',
                     'user_title': 'SGL: обзор пользователя {display}',
                     'user_description': 'Клиентский профиль {mention}\nDiscord ID: `{user_id}`',
                     'discord_field': 'Discord',
                     'nickname_field': 'Реальный никнейм',
                     'static_field': 'Статик',
                     'phone_field': 'Телефон',
                     'bank_field': 'Банковский счёт',
                     'case_count_field': 'Кейсов клиента',
                     'client_cases_field': 'Кейсы клиента',
                     'no_cases': 'У пользователя пока нет кейсов в SGL.',
                     'more_cases': '…и ещё `{count}` кейс(ов).',
                     'user_footer': '{bot_name} • SGL Bureau user overview',
                     'case_title': 'SGL Case {case_number} {emoji}',
                     'case_description': 'Статус: **{status}**\nКанал: {channel}',
                     'claim_link_field': 'Ссылка на исковое заявление',
                     'situation_field': 'Разбор ситуации',
                     'recent_events_field': 'Последние действия по кейсу',
                     'event_line': '• `{datetime}` — {action} • {actor}',
                     'case_footer': '{bot_name} • SGL Bureau case control',
                     'menu_not_for_you': 'Это меню открыто не для Вас.',
                     'user_not_found': 'Не смог найти пользователя `{user_id}` на сервере.',
                     'create_case_failed': 'Не удалось создать кейс: `{error}`',
                     'create_case_success': 'Создан кейс **{case_number}**: <#{channel_id}>',
                     'link_modal_title': 'Ссылка по кейсу {case_number}',
                     'link_modal_label': 'Ссылка на исковое заявление',
                     'link_modal_placeholder': 'https://forum.example/...',
                     'link_saved': 'Ссылка добавлена по кейсу **{case_number}**.',
                     'case_action_only_in_case_channel': 'Это действие нужно выполнить в канале '
                                                         'кейса: <#{channel_id}>.',
                     'open_channel_button': 'Открыть канал',
                     'open_claim_button': 'Открыть иск',
                     'create_case_button': 'Создать кейс',
                     'refresh_user_button': 'Обновить обзор',
                     'params_button': 'Параметры',
                     'add_link_button': 'Добавить ссылку',
                     'close_case_button': 'Закрыть кейс',
                     'refresh_case_button': 'Обновить',
                     'errors': {'only_one_arg': 'Укажите только один аргумент: либо пользователя, '
                                                'либо номер кейса.',
                                'case_not_found': 'Кейс **{case_number}** не найден в базе SGL.',
                                'no_args_in_cmd': 'Укажите пользователя или номер кейса. Например: '
                                                  '`/sg user:@User` или `/sg case:090`.',
                                'no_case_here': 'Без аргументов `/sg` работает только в канале '
                                                'конкретного кейса. В командном центре укажите '
                                                'пользователя или номер кейса.'},
                     'action': {'reserved': 'кейс зарезервирован',
                                'channel_created': 'канал создан',
                                'params_saved': 'параметры клиента сохранены',
                                'situation_saved': 'разбор ситуации сохранён',
                                'claim_link_saved': 'ссылка на иск добавлена',
                                'closed': 'кейс закрыт',
                                'archived': 'кейс перенесён в архив',
                                'error': 'ошибка кейса',
                                'receipt_created': 'чек оплаты создан',
                                'receipt_proofs_submitted': 'фотографии оплаты отправлены',
                                'receipt_confirmed': 'оплата подтверждена'},
                     'create_receipt_button': 'Создать чек'},
              'receipt': {'create_button': 'Создать чек',
                          'modal_title': 'Чек оплаты • кейс №{case_number}',
                          'where_label': 'Куда идёт обращение',
                          'where_placeholder': 'd/окружной, s/верховный, k/апелляция или кассация, '
                                               'o/обращение, e/иное',
                          'total_label': 'Общая сумма чека',
                          'total_placeholder': 'Например: 500000. Пошлина будет вычтена '
                                               'автоматически, если тип не Иное.',
                          'duty_label': 'Пошлина, если тип Иное',
                          'duty_placeholder': 'Для d/s/k/o можно оставить пустым. Для e укажите '
                                              'сумму пошлины.',
                          'no_permission': 'Создать чек может ведущий адвокат, секретарь, '
                                           'администратор или стафф SGL.',
                          'created_private': 'Чек создан и отправлен в канал кейса. ID чека: '
                                             '`{receipt_id}`.',
                          'invoice_message': '{client}\n'
                                             '# Чек оплаты SGL\n'
                                             '\n'
                                             '**Кейс:** `№{case_number}`\n'
                                             '**Куда идёт обращение:** **{court}** (`{suffix}`)\n'
                                             '**Общая сумма чека:** `{total}`\n'
                                             '\n'
                                             '### К оплате\n'
                                             '**1. Услуги адвоката:** `{lawyer_amount}`\n'
                                             '**Счёт адвоката:** `{lawyer_bank}`\n'
                                             '\n'
                                             '**2. Судебная пошлина:** `{duty_amount}`\n'
                                             '**Счёт судебной пошлины:** `{duty_bank}`\n'
                                             '\n'
                                             '### Важно\n'
                                             'После оплаты обязательно сделайте **полные скриншоты '
                                             'оплаты**: отдельно по услугам адвоката и отдельно по '
                                             'судебной пошлине. На скриншоте должны быть видны '
                                             'SMS, уведомление или экран подтверждения платежа.\n'
                                             '\n'
                                             'Нажмите кнопку ниже и отправьте две ссылки на '
                                             'фотографии оплаты.',
                          'submit_proofs_button': 'Отправить ссылки на фото',
                          'proof_modal_title': 'Ссылки на фотографии оплаты',
                          'services_proof_label': 'Фото оплаты услуг адвоката',
                          'duty_proof_label': 'Фото оплаты судебной пошлины',
                          'proof_placeholder': 'https://...',
                          'proofs_title': 'Скриншоты оплаты • кейс №{case_number}',
                          'proofs_description': 'Ссылки на оплату по чеку `#{receipt_id}` отправил '
                                                '{actor}.',
                          'services_proof_field': 'Оплата услуг адвоката',
                          'duty_proof_field': 'Оплата судебной пошлины',
                          'proofs_footer': 'SGL Payments • {datetime}',
                          'proofs_saved_private': 'Ссылки на фотографии оплаты сохранены. Ведущий '
                                                  'адвокат получил подтверждение в личные '
                                                  'сообщения.',
                          'confirm_for_lawyer': 'Проверьте оплату по чеку и подтвердите её кнопкой '
                                                'ниже.',
                          'confirm_title': 'Проверка оплаты • кейс №{case_number}',
                          'confirm_description': 'Чек `#{receipt_id}` • `{suffix}` • сумма '
                                                 '`{total}`. Подтвердить оплату может только '
                                                 'ведущий адвокат.',
                          'confirm_button': 'Оплата подтверждена',
                          'confirmed_title': 'Оплата подтверждена • кейс №{case_number}',
                          'confirmed_description': '{actor} подтвердил оплату. В названии кейса '
                                                   'будет указан суффикс `{suffix}`.',
                          'confirmed_footer': 'SGL Payments • {datetime}',
                          'confirmed_private': 'Оплата подтверждена. Канал кейса будет обновлён.',
                          'case_panel_field': 'Чеки и оплаты',
                          'case_panel_line': '`#{id}` **{suffix}** • {court} • чек `{total}` • '
                                             'адвокат `{lawyer}` • пошлина `{duty}` • {status} • '
                                             'фото: {proofs}',
                          'proofs_short_yes': 'есть',
                          'proofs_short_no': 'нет',
                          'status': {'issued': 'выставлен',
                                     'proofs_submitted': 'фото отправлены',
                                     'confirmed': 'подтверждено'},
                          'errors': {'unknown_court': 'Не понял, куда идёт обращение. Используйте '
                                                      '`d`, `s`, `k`, `o`, `e` или русские '
                                                      'варианты: `окружной`, `верховный`, '
                                                      '`кассация`, `апелляция`, `обращение`, '
                                                      '`иное`.',
                                     'total_amount': 'Общая сумма чека должна быть положительным '
                                                     'числом.',
                                     'custom_duty': 'Для типа `Иное` нужно указать сумму пошлины '
                                                    'числом. Если пошлины нет, укажите `0`.',
                                     'duty_bigger_than_total': 'Пошлина `{duty}` больше общей '
                                                               'суммы чека `{total}`. Исправьте '
                                                               'суммы.',
                                     'receipt_not_found': 'Чек не найден в базе. Возможно, '
                                                          'сообщение было создано старой версией '
                                                          'бота.',
                                     'proof_url': 'Поле **{field}** должно быть ссылкой формата '
                                                  '`https://...`.',
                                     'proof_yapix': 'Поле **{field}** не должно использовать '
                                                    'Yapix. Загрузите изображение на другой '
                                                    'хостинг.',
                                     'only_lead_lawyer_confirm': 'Подтвердить оплату может только '
                                                                 'ведущий адвокат кейса или '
                                                                 'администратор.'},
                          'invoice_content': '{client}',
                          'invoice_title': 'Чек оплаты SGL • кейс №{case_number}',
                          'invoice_description': '**Куда идёт обращение:** {court} (`{suffix}`)\n'
                                                 '**Общая сумма:** `{total}`',
                          'invoice_lawyer_field': 'Услуги адвоката',
                          'invoice_lawyer_value': '`{amount}` → счёт `{bank}`\n'
                                                  'Получатель: {lawyer}',
                          'invoice_duty_field': 'Судебная пошлина',
                          'invoice_duty_value': '`{amount}` → счёт `{bank}`',
                          'invoice_notice_field': 'Подтверждение оплаты',
                          'invoice_notice_value': 'После оплаты нажмите кнопку ниже и отправьте '
                                                  '**две ссылки на полные скриншоты**: отдельно '
                                                  'услуги адвоката и отдельно судебную пошлину. На '
                                                  'скриншотах должны быть видны SMS, уведомление '
                                                  'или экран подтверждения платежа.',
                          'invoice_footer': 'SGL Payments • выставил {author} • {datetime}',
                          'confirm_dm_content': 'Проверьте оплату по чеку `#{receipt_id}` в кейсе '
                                                '№{case_number}.',
                          'confirm_dm_sent_channel': 'Проверка оплаты отправлена ведущему адвокату '
                                                     '{lawyer} в личные сообщения.',
                          'confirm_dm_failed_channel': 'Не удалось отправить личное сообщение '
                                                       'ведущему адвокату {lawyer}. Проверьте, '
                                                       'открыты ли ЛС для бота.'}},
 'errors': {'generic_interaction_error': 'Произошла ошибка при выполнении команды.',
            'generic_interaction_error_with_id': 'Произошла ошибка при выполнении команды. Код: '
                                                 '`{error_id}`'},
 'console': {'localization_read_failed': 'Failed to read localization file {path}: {error}',
             'token_missing': 'ERROR: DISCORD_TOKEN is not set. Create .env and put your bot token '
                              'there.',
             'invalid_command_name': 'Invalid command name for {key}: {value}. Fallback used: '
                                     '{fallback}.',
             'synced_guild': 'Synced {count} slash command(s) to guild {guild_id}.',
             'synced_global': 'Synced {count} global slash command(s). Global sync can take some '
                              'time in Discord.',
             'sync_failed': 'Failed to sync slash commands: {error}',
             'db_path': 'SQLite database file: {path}',
             'db_migration': 'Legacy activity migration imported {events} event(s), {users} '
                             'user(s), {counters} counter row(s).',
             'db_migration_skipped': 'Legacy activity migration skipped: {reason}',
             'reaction_add': 'REACTION ADD: {member} {emoji}',
             'reaction_remove': 'REACTION REMOVE: {member} {emoji}',
             'voice_join': 'VOICE JOIN: {member} -> {channel}',
             'voice_leave': 'VOICE LEAVE: {member} <- {channel}',
             'voice_move': 'VOICE MOVE: {member}: {before_channel} -> {after_channel}',
             'voice_status': 'VOICE STATUS: {member}: {details}',
             'member_update': 'MEMBER UPDATE: {member} {details}',
             'member_join': 'MEMBER JOIN: {member}',
             'member_leave': 'MEMBER LEAVE: {member}',
             'presence_status': 'PRESENCE: {member} {before_status} -> {after_status}',
             'presence_activity': 'PRESENCE ACTIVITY: {member}',
             'ready_no_user': 'Bot is online, but bot.user is unavailable.',
             'ready': 'Logged in as {user} | ID: {user_id}',
             'persistent_localization': 'Persistent localization file: {path}',
             'tracking_scope': 'Tracking all members: {track_all}; track only role ID: {role_id}',
             'enabled_trackers': 'Enabled trackers: messages={messages}, '
                                 'message_edits={message_edits}, '
                                 'message_deletes={message_deletes}, reactions={reactions}, '
                                 'voice={voice}, voice_status={voice_status}, typing={typing}, '
                                 'member_updates={member_updates}, '
                                 'member_join_leave={member_join_leave}, presence={presence}, '
                                 'commands={commands}',
             'interaction_failed': 'Interaction error: {error}',
             'status_set': 'Bot status set: {status}, {activity_type}, {text}',
             'status_failed': 'Failed to set bot status: {error}'},
 'sglaudio': {'module_name': 'SGL Audio',
              'commands': {'sglaudio_name': 'sglaudio',
                           'sglaudio_description': 'Сгенерировать AI-аудио через SGL Audio'},
              'modal': {'title': 'SGL Audio Generator',
                        'prompt_label': 'Промпт для генерации музыки',
                        'prompt_placeholder': 'Например: cinematic dark jazz theme for a legal '
                                              'bureau, slow tempo, dramatic brass, no vocals'},
              'status': {'started': '🎧 Запрос принят. Генерация запущена через `{model}`.\n'
                                    'ID генерации: `{id}`.\n'
                                    'Сейчас начну обновлять таймер.',
                         'generating': '🎧 Генерация аудио через `{model}`...\n'
                                       'Прошло: **{seconds} сек.**\n'
                                       'После завершения файл будет отправлен в канал '
                                       '<#{channel_id}>.',
                         'done': '✅ Аудио готово.\n'
                                 'Время генерации: **{seconds} сек.**\n'
                                 'Файл отправлен в канал <#{channel_id}>.\n'
                                 'ID генерации: `{id}`.',
                         'failed': '❌ Генерация не завершилась.\n'
                                   'Прошло: **{seconds} сек.**\n'
                                   'ID генерации: `{id}`.\n'
                                   'Ошибка: ```{error}```',
                         'generating_channel': '🎧 Генерация аудио через `{model}`...\n'
                                               'Прошло: **{seconds} сек.**\n'
                                               'После завершения файл будет отправлен в канал '
                                               '<#{channel_id}>.',
                         'done_channel': '✅ Аудио готово.\n'
                                         'Время генерации: **{seconds} сек.**\n'
                                         'Файл отправлен в канал <#{channel_id}>.\n'
                                         'ID генерации: `{id}`.',
                         'queued': '⏳ Генератор сейчас занят другой генерацией. Ваш запрос '
                                   'поставлен в очередь.\n'
                                   'Модель: `{model}`',
                         'cooldown': '⏱️ Жду техническую паузу перед следующим запросом к '
                                     'OpenRouter: **{seconds} сек.**\n'
                                     'Это снижает шанс, что провайдер вернёт только текст вместо '
                                     'аудио.\n'
                                     'Модель: `{model}`',
                         'retrying_start': 'OpenRouter ещё не начал аудиогенерацию. Повторяю '
                                           'запрос... попытка `{attempts}`.'},
              'dm': {'title': 'SGL Audio • готовый файл',
                     'description': 'Генерация завершена за **{seconds} сек.**\nМодель: `{model}`',
                     'prompt_field': 'Промпт',
                     'footer': 'T-Mod • SGL Audio • ID генерации: {id}'},
              'errors': {'guild_only': 'Команда работает только на сервере Discord.',
                         'wrong_channel': 'Команду можно использовать только в канале '
                                          '<#{channel_id}>.',
                         'no_permission': 'Команду может использовать только роль <@&{role_id}> '
                                          'или администратор.',
                         'api_key_missing': 'Не указан `OPENROUTER_API_KEY` в постоянном `.env` '
                                            'файле. Добавьте ключ в '
                                            '`C:\\Users\\Admin\\Documents\\SGLDiscordBot\\.env` и '
                                            'перезапустите бота.',
                         'empty_prompt': 'Промпт не может быть пустым.',
                         'openrouter_http': 'OpenRouter вернул HTTP `{status}`. Ответ: {body}',
                         'no_json': 'OpenRouter вернул не JSON и не аудиофайл. Ответ: {body}',
                         'no_audio_in_response': 'OpenRouter ответил, но аудиофайл не найден в '
                                                 'ответе. Фрагмент ответа: {body}',
                         'file_too_large': 'Файл слишком большой для отправки в Discord. Размер: '
                                           '{size} байт, лимит: {limit} байт. Файл сохранён на '
                                           'сервере: {path}',
                         'modal_failed': 'Ошибка формы SGL Audio: `{error}`',
                         'no_audio_stream': 'OpenRouter ответил потоково, но не прислал '
                                            'аудиоданные. Бот попробует повторить запрос '
                                            'автоматически. Если ошибка повторяется, провайдер '
                                            'временно отдаёт только текст вместо audio output.',
                         'text_only_response': 'OpenRouter вернул только текст, а не аудиофайл. '
                                               'Фрагмент текста: {body}',
                         'output_channel_not_found': 'Не найден канал для отправки аудио '
                                                     '<#{channel_id}>. Ошибка: `{error}`',
                         'output_channel_not_sendable': 'Канал <#{channel_id}> не поддерживает '
                                                        'отправку сообщений.',
                         'output_channel_send_failed': 'Не удалось отправить аудиофайл в канал '
                                                       '<#{channel_id}>. Ошибка: `{error}`',
                         'no_audio_stream_retry_exhausted': 'OpenRouter не прислал аудиоданные для '
                                                            'модели `{model}` после попытки '
                                                            '`{attempts}`. Подробности:\n'
                                                            '{body}',
                         'no_audio_after_retries': 'OpenRouter несколько раз ответил без аудио или '
                                                   'временной ошибкой. Всего попыток: '
                                                   '`{attempts}`. Последние ответы:\n'
                                                   '{body}'},
              'channel': {'content': '{requester}, готово. Аудиофайл SGL Audio прикреплён ниже. ID '
                                     'генерации: `{id}`.',
                          'title': 'SGL Audio • готовый файл',
                          'description': 'Генерация завершена за **{seconds} сек.**\n'
                                         'Модель: `{model}`\n'
                                         'Запросил: {requester}',
                          'prompt_field': 'Промпт',
                          'footer': 'T-Mod • SGL Audio • ID генерации: {id}'}}}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


_locale_cache: dict[str, Any] = deepcopy(DEFAULT_LOCALE)
_locale_mtime: float | None = None

LOCALIZATION_FILE = Path(os.getenv("LOCALIZATION_FILE", "/app/persistent/localization.json"))
LOCALIZATION_FALLBACK_FILE = Path("/app/localization.example.json")


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        with path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as exc:
        template = DEFAULT_LOCALE["console"]["localization_read_failed"]
        print(template.format(path=path, error=exc), file=sys.stderr)
        return None


def load_locale(force: bool = False) -> dict[str, Any]:
    global _locale_cache, _locale_mtime

    source = LOCALIZATION_FILE if LOCALIZATION_FILE.exists() else LOCALIZATION_FALLBACK_FILE
    try:
        current_mtime = source.stat().st_mtime if source.exists() else None
    except OSError:
        current_mtime = None

    if not force and current_mtime == _locale_mtime:
        return _locale_cache

    custom = read_json_file(source)
    _locale_cache = deep_merge(DEFAULT_LOCALE, custom or {})
    _locale_mtime = current_mtime
    return _locale_cache


def get_nested(data: dict[str, Any], dotted_key: str) -> Any:
    item: Any = data
    for part in dotted_key.split("."):
        if not isinstance(item, dict) or part not in item:
            return None
        item = item[part]
    return item


def t(locale_key: str, **kwargs: Any) -> str:
    value = get_nested(load_locale(), locale_key)
    if value is None:
        value = get_nested(DEFAULT_LOCALE, locale_key)
    if value is None:
        value = locale_key
    text = str(value)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError, IndexError):
            return text
    return text


def safe_command_name(key: str, fallback: str) -> str:
    raw = t(key).strip().lower()
    if raw == key.strip().lower():
        return fallback
    if re.fullmatch(r"[a-z0-9_-]{1,32}", raw):
        return raw
    print(t("console.invalid_command_name", key=key, value=raw, fallback=fallback), file=sys.stderr)
    return fallback


def safe_command_description(key: str, fallback: str) -> str:
    raw = t(key).strip()
    if not raw or raw == key:
        raw = fallback
    return raw[:100]


load_locale(force=True)
