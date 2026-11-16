"""Game-state rates shared by pre-match and team report interpretations."""

def rate_evidence(states, snake=False):
    total_minutes = sum(row.get('minutes', 0) for row in states)
    evidence = []
    for row in states:
        def value(camel, snake_name):
            return row.get(snake_name if snake else camel)
        matches = value('matchesEntered', 'matches_entered') or 0
        minutes = row.get('minutes', 0)
        def percent(count):
            return round(count / matches * 100, 1) if count is not None and matches > 0 and 0 <= count <= matches else None
        wins, draws, losses = (value(c, s) or 0 for c, s in (
            ('finalWins', 'final_wins'), ('finalDraws', 'final_draws'), ('finalLosses', 'final_losses')))
        evidence.append({
            'key': row['key'], 'matchesEnteringState': matches,
            'timeSharePct': round(minutes / total_minutes * 100, 1) if total_minutes > 0 else None,
            'averageSpellMinutes': value('averageSpell', 'average_spell') if row.get('segments', 0) > 0 else None,
            'goalsForPer90StateMinutes': round((value('goalsFor', 'goals_for') or 0) / minutes * 90, 2) if minutes > 0 else None,
            'goalsAgainstPer90StateMinutes': round((value('goalsAgainst', 'goals_against') or 0) / minutes * 90, 2) if minutes > 0 else None,
            'matchesMovingAheadPct': percent(value('matchesToAhead', 'matches_to_ahead')),
            'matchesReturningLevelPct': percent(value('matchesToLevel', 'matches_to_level')),
            'matchesFallingBehindPct': percent(value('matchesToBehind', 'matches_to_behind')),
            'winPct': percent(wins), 'drawPct': percent(draws), 'lossPct': percent(losses),
            'droppedPointsPct': percent(draws + losses),
        })
    return evidence


def rate_perspectives(states, lang='en', snake=False):
    """Deterministic interpretations also support saved reports without new AI calls."""
    result = {}
    for row in rate_evidence(states, snake):
        key, count = row['key'], row['matchesEnteringState']
        if not count:
            result[key] = ('Bu skor durumu için değerlendirilebilir maç bulunmuyor.' if lang == 'tr'
                           else 'No matches entered this game state in the selected matches.')
            continue
        pct = row['winPct'] if key == 'ahead' else row['drawPct'] if key == 'level' else row['winPct']
        if pct is None:
            result[key] = 'Sonuç oranı için veri bulunmuyor.' if lang == 'tr' else 'Outcome-rate data is unavailable.'
            continue
        formatted = f'{pct:.1f}'.replace('.', ',') if lang == 'tr' else f'{pct:.1f}'
        if lang == 'tr':
            text = {'ahead': f'Öne geçilen {count} maçın %{formatted} kadarı galibiyetle tamamlandı; bu oran öne geçilen maçları kazanabilme sıklığını gösteriyor.',
                    'level': f'Skorun eşit olduğu {count} maçın %{formatted} kadarı beraberlikle tamamlandı; bu oran eşitlikten maç sonuna uzanan sonuç eğilimini gösteriyor.',
                    'behind': f'Geride kalınan {count} maçın %{formatted} kadarı galibiyetle tamamlandı; bu oran geriye düşülen maçları kazanabilme sıklığını gösteriyor.'}[key]
            if count < 5: text += ' Az sayıda maça dayanıyor.'
        else:
            text = {'ahead': f'{formatted}% of the {count} matches in which the team led ended in victory, showing how often a lead translated into a final win.',
                    'level': f'{formatted}% of the {count} matches entering a level score ended in a draw, describing the final-outcome pattern after this state.',
                    'behind': f'{formatted}% of the {count} matches in which the team trailed ended in victory, showing how often a deficit was followed by a comeback win.'}[key]
            if count < 5: text += ' Based on few matches.'
        result[key] = text
    return result
