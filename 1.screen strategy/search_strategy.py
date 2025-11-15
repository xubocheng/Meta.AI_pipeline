def summarize_abstract(title, abstract, first_author):
    def reformat_author_name(author_name):
        try:
            return author_name.replace(",", "")
        except AttributeError:
            return "Unknown Author"

    formatted_author = reformat_author_name(first_author)

    # decision_prompt仍然维持原有逻辑，用于判断摘要类型
    decision_prompt = (
        f"Your task is to decide the type of summary needed based on the abstract.\n\n"
        f"Instructions:\n"
        f"- If the study primarily introduces, describes, or refines a method, technique, model, or computational approach, "
        f"with its main contribution being methodological rather than a discovery about a phenomenon, then output:\n"
        f"Output: full\n\n"
        f"- If the study primarily reports a new discovery, finding, result, or empirical outcome about a certain phenomenon, "
        f"biological entity, material property, or theoretical insight, then output:\n"
        f"Output: concise\n\n"
        f"Make your decision strictly based on the abstract content. Do not provide explanations or reasoning, "
        f"only the exact output word as instructed.\n\n"
        f"Title: {title}\nAbstract: {abstract}\n"
    )

    # full_summary_prompt不再要求使用第一作者信息，只需要两句话总结主要发现
    full_summary_prompt = (
        "In exactly two sentences, provide a high-level summary of the study’s key findings, "
        "while maintaining concrete technical terms, methodologies, and specific entities. "
        # "Do not use 'this study', 'the authors', or similar phrases as the subject; instead, use a proper noun or specific entity mentioned or implied in the abstract as the subject. "
        "Use clear and advanced language without generalizing or replacing specific methods with vague terms.\n\n"
        f"The summary should use clear, advanced language and mention the first author {formatted_author} followed by 'et al.':\n\n"
        f"Title: {title}\nAbstract: {abstract}\n\n"
        f"Summary by {formatted_author} et al.:"
    )

    # concise_summary_prompt不再要求使用第一作者信息，只需要一句话总结主要发现
    concise_summary_prompt = (
        "In two sentence, provide a precise statement of the study’s main finding without generalizing and without making the study itself the subject. "
        "Do not use 'this study', 'the authors', or similar phrases as the subject; instead, use a proper noun or specific entity mentioned or implied in the abstract as the subject of the sentence. "
        "Directly present the finding as the sentence’s focus, using advanced and specific language.\n\n"
        f"Title: {title}\nAbstract: {abstract}\n\n"
    )

    def generate_response(prompt):
        response = chat_completion(prompt)
        if response is None:
            return None

        try:
            return response.choices[0].message.content.strip().lower()
        except AttributeError:
            print("Error in chat_completion response format:", response)
            return None

    decision_response = generate_response(decision_prompt)
    print(decision_response)
    # 根据decision_response选择summary类型
    if decision_response and "full" in decision_response:
        summary_prompt = full_summary_prompt
    else:
        summary_prompt = concise_summary_prompt

    summary_response = chat_completion(summary_prompt)
    if summary_response and summary_response.choices:
        return summary_response.choices[0].message.content.strip()
    else:
        return "Summary unavailable."


# Function to check relevance and obtain keywords as reason
def is_relevant(title, abstract, topic, direction):
    combined_text = f"{title} {abstract}"

    relevance_prompt = (
        f"You are an academic expert specializing in the field of {topic}. Your task is to determine if the following paper is relevant to the research direction described as '{direction}'.\n\n"
        "Please follow this reasoning process:\n"
        "1. Carefully read the paper's title and abstract.\n"
        "2. Identify the core research area, methodology, results, or focal points presented in the paper.\n"
        "3. Compare these core elements to the given research direction. Consider whether the paper directly addresses, contributes to, or is closely aligned with the stated direction.\n"
        "4. If the paper aligns conceptually, methodologically, or thematically with the direction, then it is relevant. If it is only tangential or unrelated, it is not relevant.\n"
        "5. From the text, select the main keywords that strongly indicate relevance (if relevant). These keywords should be key concepts, terms, or phrases that link the paper’s content to the given research direction.\n"
        "6. If not relevant, you can provide no keywords or give a brief note indicating no strong linkage.\n\n"
        "You must provide the answer in the following exact format:\n"
        "Relevance: True or False\n"
        "Keywords: [Comma-separated keywords]\n\n"
        f"Title: {title}\n"
        f"Abstract: {abstract}\n"
    )

    response = chat_completion(relevance_prompt)
    if response is None:
        return False, "Relevance check unavailable due to server error."

    try:
        response_text = response.choices[0].message.content
        relevance = "True" in response_text
        keywords = response_text.split("Keywords:")[-1].strip() if "Keywords:" in response_text else ""
        return relevance, keywords
    except AttributeError:
        print("Error in chat_completion response format:", response)
        return False, "Relevance check failed"