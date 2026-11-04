package main

func main() {
	url, err := start(AppPort)
	if err != nil {
		panic(err)
	}
	runWindow(url)
}
