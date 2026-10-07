import pytest

from spieltag.ratings import EloConfig, EloRatings, expected_home_score, goal_multiplier

CONFIG = EloConfig()


def test_equal_clubs_favour_the_home_side_only_by_the_home_advantage():
    assert expected_home_score(1500, 1500, EloConfig(home_advantage=0.0)) == pytest.approx(0.5)
    assert expected_home_score(1500, 1500, CONFIG) > 0.5


def test_a_stronger_club_is_expected_to_do_better():
    assert expected_home_score(1700, 1500, CONFIG) > expected_home_score(1500, 1500, CONFIG)
    assert expected_home_score(1500, 1700, CONFIG) < expected_home_score(1500, 1500, CONFIG)


@pytest.mark.parametrize(
    ("goal_difference", "expected"),
    [(0, 1.0), (1, 1.0), (-1, 1.0), (2, 1.5), (-2, 1.5), (3, 1.75), (4, 1.875)],
)
def test_goal_multiplier(goal_difference, expected):
    assert goal_multiplier(goal_difference) == pytest.approx(expected)


def test_an_update_moves_points_between_the_two_clubs_and_nothing_else():
    ratings = EloRatings()
    ratings.start_season()
    ratings.rating(3)  # a bystander
    ratings.update(1, 2, 2, 0)
    assert ratings.rating(1) > 1500 > ratings.rating(2)
    assert ratings.rating(1) + ratings.rating(2) == pytest.approx(3000)
    assert ratings.rating(3) == 1500


def test_a_bigger_win_moves_ratings_more():
    narrow, big = EloRatings(), EloRatings()
    for ratings in (narrow, big):
        ratings.start_season()
    narrow.update(1, 2, 1, 0)
    big.update(1, 2, 4, 0)
    assert big.rating(1) > narrow.rating(1)


def test_an_unexpected_result_moves_ratings_more_than_an_expected_one():
    expected, upset = EloRatings(), EloRatings()
    for ratings in (expected, upset):
        ratings.start_season()
        ratings._ratings.update({1: 1700.0, 2: 1500.0})
    expected.update(1, 2, 1, 0)  # the favourite wins
    upset.update(2, 1, 1, 0)  # the underdog wins away... as the home side
    assert abs(upset.rating(2) - 1500) > abs(expected.rating(1) - 1700)


def test_a_draw_between_equal_clubs_still_hands_the_away_side_points():
    ratings = EloRatings()
    ratings.start_season()
    ratings.update(1, 2, 1, 1)  # the home side was expected to do better than a draw
    assert ratings.rating(1) < 1500 < ratings.rating(2)


def test_ratings_are_pulled_back_towards_the_mean_between_seasons():
    ratings = EloRatings()
    ratings.start_season()
    ratings._ratings.update({1: 1700.0, 2: 1300.0})
    ratings.start_season()
    assert ratings.rating(1) == pytest.approx(1500 + 0.75 * 200)
    assert ratings.rating(2) == pytest.approx(1500 - 0.75 * 200)


def test_the_first_season_does_not_regress_anything():
    ratings = EloRatings()
    ratings.start_season()
    ratings._ratings[1] = 1700.0
    assert ratings.rating(1) == 1700.0


def test_clubs_seen_for_the_first_time_after_the_first_season_start_lower():
    ratings = EloRatings()
    ratings.start_season()
    assert ratings.rating(1) == CONFIG.initial
    ratings.start_season()
    assert ratings.rating(2) == CONFIG.newcomer
