from nltk.tokenize import word_tokenize
import numpy as np

QUADRATIC_COEFFICIENTS: np.ndarray = np.loadtxt(
    "quadratic_coefficients.csv",
    delimiter=","
)
QUADRATIC_POLYNOMIAL: np.poly1d = np.poly1d(QUADRATIC_COEFFICIENTS)

def count_tokens_in_text(text: str):
    tokens = word_tokenize(text)
    return len(tokens)

def count_words_in_text(text: str):
    if not text:
        return 0
    words = text.split()
    return len(words)    

def get_duration_from_message(message: str)-> float:
    num_tokens = count_tokens_in_text(message)
    quadratic_fit = QUADRATIC_POLYNOMIAL
    duration_seconds = quadratic_fit(num_tokens)
    return duration_seconds

def should_finish_sentence(
        message: str, 
        seconds_spoken: float,
        threshold: float = 0.8
    ):
    min_words_to_interrupt = 4
    if count_words_in_text(message) < min_words_to_interrupt:
        return True
    else:
        duration_seconds = get_duration_from_message(message)
        print(f"duration_seconds: {duration_seconds}")
        return seconds_spoken > threshold*duration_seconds