/*
* Copyright 2023 Redpanda Data, Inc.
*
* Use of this software is governed by the Business Source License
* included in the file licenses/BSL.md
*
* As of the Change Date specified in that file, in accordance with
* the Business Source License, use of this software will be governed
* by the Apache License, Version 2.0
 */

package transform

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/redpanda-data/redpanda/src/go/rpk/pkg/cli/transform/buildpack"
	"github.com/redpanda-data/redpanda/src/go/rpk/pkg/cli/transform/project"
	"github.com/redpanda-data/redpanda/src/go/rpk/pkg/cli/transform/template"
	"github.com/redpanda-data/redpanda/src/go/rpk/pkg/out"
	"github.com/spf13/afero"
	"github.com/spf13/cobra"
)

type optionalBool struct {
	ok *bool
}

func (b *optionalBool) Set(s string) error {
	if strings.ToLower(s) == "prompt" {
		b.ok = nil
		return nil
	}
	v, err := strconv.ParseBool(s)
	b.ok = &v
	return err
}

func (*optionalBool) Type() string {
	return "bool"
}

func (b *optionalBool) String() string {
	if b.ok == nil {
		return "prompt"
	}
	return strconv.FormatBool(*b.ok)
}

func (*optionalBool) IsBoolFlag() bool { return true }

func newInitializeCommand(fs afero.Fs) *cobra.Command {
	var (
		name    string
		lang    project.WasmLang
		install optionalBool
	)
	cmd := &cobra.Command{
		Use:   "init [DIRECTORY]",
		Short: "Initialize a transform",
		Long: `Initialize a transform.

Creates a new transform using a template in the current directory.

A new directory can be created by specifying it in the command, like:

  rpk transform init foobar

Will initialize a transform project in the foobar directory.
		`,
		Args: cobra.MaximumNArgs(1),
		Run: func(cmd *cobra.Command, args []string) {
			path, err := computeProjectDirectory(fs, args)
			out.MaybeDie(err, "unable to determine project directory: %v", err)
			c := filepath.Join(path, project.ConfigFileName)
			ok, err := afero.Exists(fs, c)
			out.MaybeDie(err, "unable to determine if %q exists: %v", c, err)
			if ok {
				out.Die("there is already a transform at %q, please delete it before retrying", c)
			}
			if name == "" {
				suggestion := filepath.Base(path)
				if suggestion == "." {
					suggestion = ""
				}
				name, err = out.PromptWithSuggestion(suggestion, "name this transform:")
				out.MaybeDie(err, "unable to determine project name: %v", err)
				if name == "" {
					out.Die("transform name is required")
				}
			}
			if lang == "" {
				langVal, err := out.Pick(project.AllWasmLangs, "select a language:")
				out.MaybeDie(err, "unable to determine transform language: %v", err)
				lang = project.WasmLang(langVal)
			}
			p := transformProject{Name: name, Lang: lang, Path: path}

			fmt.Printf("generating project in %s...\n", p.Path)
			err = executeGenerate(fs, p)
			out.MaybeDie(err, "unable to generate all manifest files: %v", err)
			fmt.Println("created project in", p.Path)

			if install.ok == nil {
				ok, err = out.Confirm("install dependencies?")
				install.ok = &ok
				out.MaybeDie(err, "unable to determine if dependencies should be installed: %v", err)
			}
			if *install.ok {
				fmt.Println("installing dependencies...")
				err = installDeps(cmd.Context(), fs, p)
				out.MaybeDie(err, "unable to install dependencies: %v", err)
				fmt.Println("dependencies installed")
			}

			cwd, err := os.Getwd()
			out.MaybeDie(err, "unable to get current directory: %v", err)
			fmt.Println("deploy your transform using:")
			if cwd != path {
				rel, err := filepath.Rel(cwd, path)
				if err == nil {
					fmt.Println("\tcd", rel)
				}
			}
			fmt.Println("\trpk transform build")
			fmt.Println("\trpk transform deploy")
		},
	}
	cmd.Flags().VarP(&lang, "language", "l", "The language used to develop the transform")
	cmd.Flags().Var(&install, "install-deps", "If dependencies should be installed for the project")
	cmd.Flags().StringVar(&name, "name", "", "The name of the transform")
	return cmd
}

func computeProjectDirectory(fs afero.Fs, args []string) (string, error) {
	var path string
	cwd, err := os.Getwd()
	if err != nil {
		return "", fmt.Errorf("unable to determine current working directory: %v", err)
	}
	cwd, err = filepath.Abs(cwd)
	if err != nil {
		return "", fmt.Errorf("unable to determine absolute path for %q: %v", path, err)
	}
	if len(args) == 0 {
		return cwd, nil
	}
	path = args[0]
	ok, err := afero.Exists(fs, path)
	if err != nil {
		return "", fmt.Errorf("unable to determine if %q exists: %v", path, err)
	}
	if !ok {
		if err := fs.MkdirAll(path, os.ModeDir|os.ModePerm); err != nil {
			return "", fmt.Errorf("unable to create directory %q: %v", path, err)
		}
	}
	path, err = filepath.Abs(path)
	if err != nil {
		return "", fmt.Errorf("unable to determine absolute path for %q: %v", path, err)
	}
	f, err := fs.Stat(path)
	if err != nil {
		return "", fmt.Errorf("unable to determine if %q exists: %v", path, err)
	}
	if !f.IsDir() {
		return "", fmt.Errorf("please remove file %q to initialize a transform there", path)
	}
	return path, nil
}

type transformProject struct {
	Name string
	Path string
	Lang project.WasmLang
}

type genFile struct {
	name    string
	content string
}

func generateManifest(p transformProject) (map[string][]genFile, error) {
	if p.Lang == project.WasmLangTinygo {
		rpConfig, err := project.MarshalConfig(project.Config{Name: p.Name, Language: p.Lang})
		if err != nil {
			return nil, err
		}
		return map[string][]genFile{
			p.Path: {
				genFile{name: project.ConfigFileName, content: string(rpConfig)},
				genFile{name: "transform.go", content: template.WasmGoMain()},
				genFile{name: "go.mod", content: template.WasmGoModule(p.Name)},
				genFile{name: "README.md", content: template.WasmGoReadme()},
			},
		}, nil
	}
	return nil, fmt.Errorf("unknown language %q", p.Lang)
}

func executeGenerate(fs afero.Fs, p transformProject) error {
	manifest, err := generateManifest(p)
	if err != nil {
		return err
	}
	for dir, templates := range manifest {
		if err := fs.MkdirAll(dir, 0o755); err != nil {
			return err
		}
		for _, template := range templates {
			file := filepath.Join(dir, template.name)
			if err := afero.WriteFile(fs, file, []byte(template.content), os.FileMode(0o644)); err != nil {
				return err
			}
		}
	}
	return nil
}

func installDeps(ctx context.Context, fs afero.Fs, p transformProject) error {
	if p.Lang == project.WasmLangTinygo {
		g, err := exec.LookPath("go")
		if err != nil {
			return fmt.Errorf("go is not available on $PATH, please download and install it: https://go.dev/doc/install")
		}
		runGoCli := func(args ...string) error {
			c := exec.CommandContext(ctx, g, args...)
			c.Stderr = os.Stderr
			c.Stdin = os.Stdin
			c.Stdout = os.Stdout
			c.Dir = p.Path
			return c.Run()
		}
		if err := runGoCli("get", "github.com/redpanda-data/redpanda/src/go/transform-sdk"); err != nil {
			return fmt.Errorf("unable to go get redpanda transform-sdk: %v", err)
		}
		if err := runGoCli("mod", "tidy"); err != nil {
			return fmt.Errorf("unable to run go mod tidy: %v", err)
		}
		if _, err := buildpack.Tinygo.Install(ctx, fs); err != nil {
			return fmt.Errorf("unable to install tinygo buildpack: %v", err)
		}
		return nil
	}
	return fmt.Errorf("Unknown language %q", p.Lang)
}
