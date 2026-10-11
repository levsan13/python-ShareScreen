# ShareScreen

Compartilhe a tela inteira ou uma única janela do Windows, com o áudio do PC, para qualquer navegador da rede local. Quem assiste não instala nada: basta abrir `http://<ip-do-pc>:8080`.

É um único arquivo Python, com uma pequena interface para escolher o que transmitir.

## Recursos

- Transmite a **tela inteira** (uma tela, ou todas, se houver mais de um monitor) ou **uma janela específica**.
- Janelas são capturadas mesmo quando estão encobertas por outras.
- Transmite o **áudio que está tocando no PC**.
- Troca de fonte e de qualidade **sem interromper** a transmissão.
- Mostra o ponteiro do mouse e o número de espectadores conectados.
- A página do espectador tem controle de volume e tela cheia, e reconecta sozinha se a transmissão for reiniciada.

## Requisitos

- Windows 10 ou 11
- Python 3.10 ou superior (testado no 3.14)
- [Pillow](https://pypi.org/project/pillow/) e [PyAudioWPatch](https://pypi.org/project/PyAudioWPatch/)

## Instalação

Clone ou baixe este repositório e, na pasta do projeto, instale as dependências:

```bash
pip install -r requirements.txt
```

Sem o PyAudioWPatch o programa funciona, mas só com vídeo.

## Uso

```bash
python sharescreen.py
```

1. Escolha na lista o que compartilhar: a tela inteira ou uma das janelas abertas.
2. Marque ou desmarque **Compartilhar o áudio do PC** e escolha a qualidade.
3. Clique em **Iniciar compartilhamento**.
4. Passe o endereço exibido (por exemplo `http://192.168.0.10:8080`) para quem vai assistir.

Na primeira execução o Firewall do Windows pergunta se o Python pode aceitar conexões. Permita em **redes privadas**; sem isso, outros aparelhos não conseguem conectar.

Para usar outra porta:

```bash
python sharescreen.py --port 8081
```

### Para quem assiste

Abra o endereço em um navegador de um aparelho na mesma rede. O vídeo começa sozinho. Para ouvir o som é preciso **clicar uma vez na página**, porque os navegadores bloqueiam áudio automático.

### Qualidade

| Opção | Resolução                  | Quadros por segundo |
|-------|----------------------------|---------------------|
| Alta  | original                   | 20                  |
| Média | até 1600 px de largura     | 15                  |
| Baixa | até 1024 px de largura     | 10                  |

Quando a imagem não muda, quase nada é enviado. Com a tela inteira mudando o tempo todo (um vídeo em 1080p, por exemplo), a opção Alta pode usar dezenas de Mbit/s; em Wi-Fi fraco, prefira Média ou Baixa.

## Limitações

- **Somente Windows.** A captura usa GDI e WASAPI.
- **O áudio é o do PC inteiro**, não apenas o da janela escolhida.
- **No próprio PC que transmite, a página fica sem áudio.** O som tocado pela página seria capturado de novo e viraria eco, então para espectadores locais vai só o vídeo. Para ouvir, use outro aparelho.
- **Não há senha nem criptografia.** Qualquer pessoa na mesma rede que abrir o endereço assiste. Use apenas em redes confiáveis.
- Janelas minimizadas não podem ser capturadas; o espectador vê um aviso até a janela ser restaurada.
- Algumas janelas com conteúdo protegido ou jogos em tela cheia exclusiva podem aparecer pretas.
- O atraso é pequeno, mas existe: serve para apresentações, aulas e suporte, não para jogos.

## Problemas comuns

| Sintoma | O que fazer |
|---------|-------------|
| "Não foi possível iniciar o servidor na porta 8080" | Outro programa já usa a porta. Feche-o ou rode com `--port` e outro número. |
| Outros aparelhos não abrem o endereço | Confira se estão na mesma rede e se o Firewall do Windows liberou o Python em redes privadas. |
| Vídeo aparece, mas sem som | Clique na página. Confira também se algo está tocando no PC e se você não está assistindo do próprio PC. |
| Opção de áudio aparece como "Áudio indisponível" | Instale o PyAudioWPatch: `pip install PyAudioWPatch`. |
| O som não acompanha a troca de fones ou caixas de som | O dispositivo de saída é escolhido ao iniciar. Pare e inicie o compartilhamento de novo. |

## Como funciona

- **Vídeo:** a tela é copiada com `BitBlt` e as janelas com `PrintWindow`. Cada quadro vira um JPEG e é enviado como MJPEG (`multipart/x-mixed-replace`), que o navegador exibe em um `<img>` comum. Quadros iguais ao anterior não são recodificados.
- **Áudio:** capturado do dispositivo de saída padrão por loopback WASAPI e enviado como PCM de 16 bits. A página recebe os dados com `fetch` e os reproduz com a Web Audio API.
- **Servidor:** `http.server` da biblioteca padrão, com uma thread por espectador.

| Rota      | Conteúdo                                          |
|-----------|---------------------------------------------------|
| `/`       | Página do espectador                              |
| `/stream` | Vídeo em MJPEG                                    |
| `/audio`  | Áudio em PCM bruto                                |
| `/status` | JSON com a sessão atual e o formato do áudio      |

## Licença

Distribuído sob a licença MIT. Veja o arquivo [LICENSE](LICENSE).
