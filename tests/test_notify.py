import pytest

from planerka.mail.fake import FakeMailer
from planerka.notify import assert_no_pii, send_alert, send_alert_safe


@pytest.fixture
def mailer():
    return FakeMailer()


def test_alert_text_contains_no_names(directory, mailer):
    with pytest.raises(ValueError):
        send_alert(mailer, ['dev@example.test'], 'masking', 5, 'остался Смирнов', directory)
    assert mailer.sent == []


def test_declined_name_and_yo_detected(directory):
    with pytest.raises(ValueError) as e:
        assert_no_pii('проверь у Кузнецовой', directory)
    assert 'Кузнецов' not in str(e.value)


def test_first_name_variant_detected(directory):
    with pytest.raises(ValueError):
        assert_no_pii('звонил Ольге', directory)


@pytest.mark.parametrize('text', ['почта a.b@example.test', 'тел +7 913 123-45-67', 'ИНН 2222000011'])
def test_contacts_detected(directory, text):
    with pytest.raises(ValueError):
        assert_no_pii(text, directory)


def test_clean_text_passes(directory):
    assert_no_pii('источник 5, задачи И-012, И-013, ошибок 3', directory)


def test_alert_sent_to_all_recipients(directory, mailer):
    send_alert(mailer, ['a@example.test', 'b@example.test'], 'masking', 5, 'вторая проверка нашла 2', directory)
    assert [m['to'] for m in mailer.sent] == ['a@example.test', 'b@example.test']
    m = mailer.sent[0]
    assert m['subject'] == 'Планёрка: алерт masking'
    assert 'masking' in m['body'] and '5' in m['body'] and 'вторая проверка нашла 2' in m['body']


def test_alert_without_source_shows_dash(directory, mailer):
    send_alert(mailer, ['a@example.test'], 'disk', None, 'мало места', directory)
    assert 'Источник: —' in mailer.sent[0]['body']


def test_alert_without_recipients_is_silent(directory, mailer):
    send_alert(mailer, [], 'disk', None, 'мало места', directory)
    assert mailer.sent == []


def test_safe_alert_hides_pii_detail(directory, mailer):
    assert send_alert_safe(mailer, ['a@example.test'], 'masking', 5, 'остался Смирнов', directory)
    body = mailer.sent[0]['body']
    assert 'текст скрыт: содержал ПДн' in body and 'Смирнов' not in body


def test_safe_alert_swallows_mail_error(directory, caplog):
    class Broken:
        def send(self, *a, **k):
            raise OSError('smtp down')

    assert send_alert_safe(Broken(), ['a@example.test'], 'disk', None, 'мало места', directory) is False
    assert 'OSError' in caplog.text
