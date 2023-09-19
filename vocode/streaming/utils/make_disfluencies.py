import random

def make_disfluency(message: str):
    pre_words_list = [
        "this",
        "that",
        "but"
        ]
    
    # Split the text into words
    words = message.split()

    # Iterate through the words and insert "um" after "this" and "that"
    for i, word in enumerate(words):
        if word.lower() in pre_words_list:
            # Randomly choose between "uh" and "um" with a 50% probability
            if random.random() < 0.5:
                words.insert(i + 1, "uh -")
            else:
                words.insert(i + 1, "um -")

    # Recreate the modified text
    modified_text = ' '.join(words)

    return modified_text