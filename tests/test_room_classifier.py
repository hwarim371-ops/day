import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import test_manager as fixtures
import naver_room_booking_upgraded as core
import booking_manager as manager
from room_classifier import classify, partition, PARSER_VERSION
from booking_store import entry_key, fingerprint


def card(title, item='one', labels=(), text=None):
    return {'title':title, 'itemKey':'product:'+item, 'schemaVersion':3, 'unitEvidence':True,
            'overnightEvidence':True, 'statusLabels':list(labels),
            'text':text or title+'\n캠핑사이트, 기준 2인 (최대 4인)\n최소 1박'}


class ClassificationTests(unittest.TestCase):
    def test_notice_with_price_and_capacity_is_not_room(self):
        for title in ['공지사항', '입실안내 및 이용수칙', '장박문의 선착순 예약중', '단체&워크샵(구릉지 전체이용)문의', '예약안내', '쿠폰받기']:
            self.assertEqual(classify(card(title, labels=['예약마감']))[0], 'excluded', title)

    def test_day_use_not_overnight(self):
        for title in ['데이유즈', '당일 캠크닉(9시간 이용)', '바베큐장(캠핑식당)1부 오전권 2인세트', 'Day Use']:
            self.assertEqual(classify(card(title))[0], 'excluded', title)
        self.assertEqual(classify(card('마당 A', text='마당 A\n데이유즈\n6시간 이용, 데크'))[0], 'excluded')

    def test_group_and_construction_require_review(self):
        for title in ['오지캠핑 공사중', '전체이용 카라반&캠핑카 마운틴뷰가온', '평일 A~D구역 1박예약 (주말 1박은 캠핏에서 가능)']:
            self.assertEqual(classify(card(title))[0], 'review', title)

    def test_room_description_does_not_exclude_real_room(self):
        self.assertEqual(classify(card('햇빛동', text='햇빛동\n최소 1박\n공지: 예약마감 후 취소 문의는 전화'))[0], 'room')
        self.assertEqual(classify(card('[평일 BBQ 무료] 잉그락풀글램핑 G-1'))[0], 'room')

    def test_renamed_item_remembers_exclusion_by_id(self):
        rules={'product:one':{'mode':'exclude'}}
        self.assertEqual(classify(card('새 이름'), rules), ('excluded', '사용자가 제외한 상품'))
        self.assertEqual(classify(card('새 이름', 'two'), rules)[0], 'room')

    def test_explicit_confirmation_handles_exception(self):
        self.assertEqual(classify(card('전체이용 카라반'), {'product:one':{'mode':'include'}})[0], 'room')

    def test_unknown_evidence_not_counted(self):
        item=card('상품');item['overnightEvidence']=False
        self.assertEqual(classify(item)[0], 'review')

    def test_description_line_is_not_title(self):
        self.assertEqual(classify(card('글램핑, 기준 2인 (최대 4인)'))[0], 'excluded')

    def test_notice_and_page_mentions_never_match(self):
        snapshot={'roomCards':[card('공지사항', labels=['예약마감'], text='공지사항\nA-1 예약마감')],
                  'candidates':[{'text':'A-1 예약마감','context':'A-1 예약마감','textKey':'A-1예약마감'}]}
        entry=core.DbEntry(2, '123456789', '', 'Test', '', ['A-1'])
        r=core.analyze_entry_snapshot(entry,snapshot,core.normalize_words(core.DEFAULT_STATUS_WORDS),'matched','')
        self.assertEqual((r.matched_count,r.reserved_count),(0,0))

    def test_only_own_status_badge_counts(self):
        items=[card('A-1','one',['예약마감']),card('A-2','two',[],text='A-2\n예약마감 이후 환불 불가\n최소 1박'),card('공지사항','three',['예약마감'])]
        entry=core.DbEntry(2,'123456789','','Test','',['A-1','A-2'])
        r=core.analyze_entry_snapshot(entry,{'roomCards':items},core.normalize_words(core.DEFAULT_STATUS_WORDS),'matched','')
        self.assertEqual((r.matched_count,r.reserved_count),(2,1))

    def test_duplicate_titles_are_ambiguous(self):
        items=[card('A-1','one',['예약마감']),card('A-1','two',[])]
        entry=core.DbEntry(2,'123456789','','Test','',['A-1'])
        r=core.analyze_entry_snapshot(entry,{'roomCards':items},core.normalize_words(core.DEFAULT_STATUS_WORDS),'matched','')
        self.assertEqual(r.matched_count,0)

    def test_promotional_duplicate_does_not_create_room_or_mark_it_closed(self):
        regular = card("프라이빗 감성글램핑 -1(TV구비, 개별주방, 개별주차", "/booking/3/bizes/1/items/2")
        promo = card("[반짝특가] 프라이빗 감성글램핑 -1(TV구비, 개별주", "/booking/3/bizes/1/items/3", ['예약마감'])
        snapshot = {"roomCards": [promo, regular]}
        groups = partition(snapshot)
        self.assertEqual([c["itemKey"] for c in groups["room"]], [regular["itemKey"]])
        self.assertEqual([c["itemKey"] for c in groups["excluded"]], [promo["itemKey"]])
        entry = core.DbEntry(2,"123456789","","Test","",[regular["title"]])
        result = core.analyze_entry_snapshot(entry,snapshot,core.normalize_words(core.DEFAULT_STATUS_WORDS),"matched","")
        self.assertEqual((result.matched_count,result.reserved_count),(1,0))
        self.assertEqual(len(partition(snapshot, {promo["itemKey"]: {"mode":"include"}})["room"]),2)
        self.assertFalse(partition({"roomCards": [promo]})["excluded"])
        self.assertFalse(partition({"roomCards": [promo,{**regular,"itemKey":"product:/booking/3/bizes/9/items/2"}]})["excluded"])
        self.assertFalse(partition({"roomCards": [promo,regular,{**regular,"itemKey":"product:/booking/3/bizes/1/items/4"}]})["excluded"])
        self.assertFalse(partition({"roomCards": [{**promo,"title":promo["title"].replace("개별주", "공용주방")}, regular]})["excluded"])
        self.assertFalse(partition({"roomCards": [{**promo,"title":promo["title"].replace("반짝특가","반짝특가 2박")}, regular]})["excluded"])
        self.assertEqual(len(partition({"roomCards": [{**promo,"availabilityEvidence":True,"statusLabels":[]},
                                                    {**regular,"reserved":True}]})["review"]),1)

    def test_unnumbered_student_offer_requires_same_storefront_named_glamping(self):
        regular = card("스탠다드 글램핑 A-1(더블)", "/booking/3/bizes/1/items/2")
        student = card("수험생 특별 할인 글램핑(40% 할인/수험표 필수)", "/booking/3/bizes/1/items/3")
        self.assertEqual(len(partition({"roomCards":[regular,student]})["excluded"]),1)
        self.assertEqual(len(partition({"roomCards":[student]})["review"]),1)
        numbered = {**student,"title":"수험생 특별 할인 글램핑 B-2(수험표 필수)"}
        self.assertFalse(partition({"roomCards":[regular,numbered]})["excluded"])


class RulePersistenceTests(unittest.TestCase):
    setUp = fixtures.Tests.setUp
    tearDown = fixtures.Tests.tearDown
    edit = fixtures.Tests.edit
    def test_rules_survive_restart_and_do_not_change_excel(self):
        m=manager.Manager(self.folder)
        entry=m.store.entries()[0][0]
        m.data['catalogs'][entry.company_id]={'snapshot':{'roomCards':[card('오지캠핑 공사중')]},'checked_at':'2026-09-06'}
        before=m.store.db.read_bytes()
        m.data['results'][entry_key(entry)]={'status':'정상', 'checked_at':'2099-01-01',
            'fingerprint':fingerprint(entry), 'parser_version':PARSER_VERSION,
            'rules_revision':m.rules_revision(entry.company_id)}
        self.assertFalse(next(e for e in m.state()['entries'] if e['key']==entry_key(entry))['result']['stale'])
        payload={'key':entry_key(entry),'revision':m.store.entries()[1],'rules_revision':m.rules_revision(entry.company_id),'items':[{'itemKey':'product:one','mode':'exclude'}]}
        m.set_rules(payload)
        restarted=manager.Manager(self.folder)
        self.assertEqual(restarted.rules(entry.company_id)['product:one']['mode'],'exclude')
        self.assertEqual(before,m.store.db.read_bytes())
        self.assertTrue(next(e for e in restarted.state()['entries'] if e['key']==entry_key(entry))['result']['stale'])
        with self.assertRaisesRegex(ValueError,'기준이 변경'):
            restarted.set_rules(payload)

    def test_review_cannot_be_silently_applied(self):
        m=manager.Manager(self.folder)
        entry=m.store.entries()[0][0]
        m.data['proposals'][entry_key(entry)]={'parser_version':PARSER_VERSION,'review':[{'title':'문의'}]}
        with self.assertRaisesRegex(ValueError,'확인 필요'):
            m.apply({'revision':m.store.entries()[1],'edits':[self.edit()], 'from_proposal':True})


if __name__=='__main__':
    unittest.main(verbosity=2)
