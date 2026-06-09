import unittest

from channel_analyzer import ChannelAnalyzer, _looks_like_marketplace_fallback


class ChannelAnalyzerFallbackTests(unittest.TestCase):
    def test_generic_ad_footer_does_not_make_content_marketplace(self):
        posts = [
            "Рыбалка на фидер утром: как выбрать прикормку и не перекормить точку.",
            "Ловля карася в июне: рабочая глубина, поводок и насадка.",
            "Реклама и сотрудничество - в личные сообщения. Канал за рекламу ответственность не несет.",
        ]

        result = ChannelAnalyzer()._fallback_analysis(posts, "Рыбное место")

        self.assertEqual(result["channel_type"], "content")

    def test_marketplace_link_is_strong_signal(self):
        text = "Нашли скидку: https://www.wildberries.ru/catalog/123/detail.aspx цена 599 ₽"

        self.assertTrue(_looks_like_marketplace_fallback(text))

    def test_marketplace_brand_plus_product_signal_is_enough(self):
        posts = [
            "WB находка дня: удобный органайзер для кухни, цена ниже обычной.",
            "Артикул 123456, можно заказать на Wildberries.",
        ]

        result = ChannelAnalyzer()._fallback_analysis(posts, "Находки WB")

        self.assertEqual(result["channel_type"], "marketplace")

    def test_product_words_without_marketplace_brand_stay_content(self):
        posts = [
            "Как выбрать прикормку для карася: цена не главное, важнее свежесть.",
            "Разбираем снасти, катушки и наживку без рекламы магазинов.",
            "Иногда дорогой товар хуже простой рабочей вещи из рыбацкого ящика.",
        ]

        result = ChannelAnalyzer()._fallback_analysis(posts, "Рыбное место")

        self.assertEqual(result["channel_type"], "content")


if __name__ == "__main__":
    unittest.main()
